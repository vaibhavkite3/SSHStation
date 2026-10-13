#!/usr/bin/env python3
"""
SSHStation: a refined SSH server manager for the terminal.
An Idea by Vaibhav Kite.

    sshstation              open the server list
    sshstation <name>       connect straight to a server (opens the list if several match)
    sshstation --list       print your servers (add --check to probe them)
    sshstation --edit       open the servers file in your editor
    sshstation --demo       try the interface with sample servers (nothing is saved)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import errno
import getpass
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    SelectionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

__version__ = "1.2.0"
APP_NAME = "SSHStation"
AUTHOR = "Vaibhav Kite"
PROG = os.environ.get("SSHSTATION_PROG_NAME", "sshstation")

FAVORITES = "__favorites__"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

DEFAULT_SETTINGS: Dict[str, object] = {
    "theme": "harbor",
    "sort": "name",
    "refresh_interval": 120,  # seconds between automatic reachability checks, 0 disables
    "probe_timeout": 3.0,  # seconds before a server counts as offline
    "iterm_tab_color": True,  # tint the iTerm2 tab for prod / staging / dev servers
    "iterm_badge": True,  # show the server name as an iTerm2 badge during a session
    "show_sidebar": True,
    "enter_opens_tab": True,  # Enter opens a new iTerm2 tab (or tmux window); False connects in place
    "close_tab_on_exit": True,  # close that tab when the ssh session ends
}

SORTS = [
    ("name", "Name"),
    ("recent", "Last connected"),
    ("status", "Status"),
    ("usage", "Most used"),
]

# "Night harbor": deep water, brass fittings, and navigation lamps
# (starboard green for reachable, port red for unreachable).
HARBOR = Theme(
    name="harbor",
    primary="#5EC4B6",
    secondary="#7FA3D9",
    accent="#E8B85C",
    foreground="#DCE4F0",
    background="#0A1220",
    surface="#0F1A2C",
    panel="#16243A",
    success="#3DDC84",
    warning="#F2C14E",
    error="#FF5A5F",
    dark=True,
    variables={
        "footer-key-foreground": "#E8B85C",
        "block-cursor-text-style": "none",
        "block-cursor-blurred-text-style": "none",
        "input-selection-background": "#E8B85C 35%",
    },
)

# Tag colours. Environment-style tags get fixed, meaningful colours;
# everything else gets a stable colour from the palette.
ENV_TAGS = {
    "prod": "#FF6B6E", "production": "#FF6B6E", "prd": "#FF6B6E", "live": "#FF6B6E",
    "staging": "#F2C14E", "stage": "#F2C14E", "stg": "#F2C14E", "uat": "#F2C14E",
    "qa": "#F2C14E", "preprod": "#F2C14E",
    "dev": "#3DDC84", "development": "#3DDC84", "test": "#3DDC84",
    "sandbox": "#3DDC84", "local": "#3DDC84",
}
TAG_PALETTE = ["#5EC4B6", "#7FA3D9", "#C49BE8", "#E8A87C", "#8FD3E8", "#B7D971", "#E88FB5", "#A3ACFF"]
PALETTE_DEFAULTS = {
    "primary": "#5EC4B6", "secondary": "#7FA3D9", "accent": "#E8B85C",
    "success": "#3DDC84", "warning": "#F2C14E", "error": "#FF5A5F",
    "background": "#0A1220", "foreground": "#DCE4F0",
}
TAB_COLORS = {"#FF6B6E": (210, 60, 64), "#F2C14E": (214, 150, 30), "#3DDC84": (38, 160, 90)}


# --------------------------------------------------------------------------- model


def normalize_tags(value) -> List[str]:
    if isinstance(value, str):
        parts = re.split(r"[,\s]+", value)
    else:
        parts = list(value or [])
    seen: List[str] = []
    for part in parts:
        tag = str(part).strip().lstrip("#").lower()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def tag_color(tag: str) -> str:
    if tag in ENV_TAGS:
        return ENV_TAGS[tag]
    return TAG_PALETTE[sum(ord(c) * (i + 1) for i, c in enumerate(tag)) % len(TAG_PALETTE)]


def split_target(text: str) -> Tuple[str, str, Optional[int]]:
    """Split `user@host:port` (or `[v6addr]:port`) into its parts."""
    user, host, port = "", text.strip(), None
    if "@" in host:
        user, host = host.rsplit("@", 1)
    bracket = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", host)
    if bracket:
        host = bracket.group(1)
        port = int(bracket.group(2)) if bracket.group(2) else None
    elif host.count(":") == 1:
        name, _, maybe_port = host.partition(":")
        if maybe_port.isdigit():
            host, port = name, int(maybe_port)
    return user, host, port


KEY_ALIASES = {
    "hostname": "host", "address": "host", "ip": "host",
    "username": "user", "login": "user",
    "agent_forwarding": "forward_agent", "forwardagent": "forward_agent", "agent": "forward_agent",
    "identity": "identity_file", "identityfile": "identity_file", "key": "identity_file",
    "proxyjump": "jump_host", "jump": "jump_host", "bastion": "jump_host",
    "options": "extra_args", "ssh_options": "extra_args",
}


def as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def stable_id(name: str, host: str, port: int) -> str:
    return "h" + hashlib.sha1(f"{name}|{host}|{port}".encode()).hexdigest()[:11]


@dataclass
class Server:
    name: str
    host: str
    port: int = 22
    user: str = ""
    tags: List[str] = field(default_factory=list)
    forward_agent: bool = False
    identity_file: str = ""
    jump_host: str = ""
    extra_args: str = ""
    favorite: bool = False
    last_connected: Optional[float] = None
    connect_count: int = 0
    created_at: Optional[float] = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_dict(cls, data: dict) -> "Server":
        """Build a server from a saved or hand-written entry. Only `host` is required."""
        if not isinstance(data, dict):
            raise ValueError("each server must be a {...} object")
        data = {KEY_ALIASES.get(str(k).lower(), str(k).lower()): v for k, v in data.items()}
        host = str(data.get("host") or "").strip()
        if not host:
            raise ValueError("it has no host")
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        clean["host"] = host
        clean["name"] = str(data.get("name") or host).strip()
        try:
            clean["port"] = int(data.get("port") or 22)
        except (TypeError, ValueError):
            raise ValueError(f"its port “{data.get('port')}” isn't a number")
        if not 1 <= clean["port"] <= 65535:
            raise ValueError(f"its port {clean['port']} is outside 1–65535")
        for key in ("user", "identity_file", "jump_host", "extra_args"):
            clean[key] = str(clean.get(key) or "").strip()
        clean["tags"] = normalize_tags(clean.get("tags"))
        clean["forward_agent"] = as_bool(clean.get("forward_agent", False))
        clean["favorite"] = as_bool(clean.get("favorite", False))
        try:
            clean["connect_count"] = int(clean.get("connect_count") or 0)
        except (TypeError, ValueError):
            clean["connect_count"] = 0
        for key in ("last_connected", "created_at"):
            try:
                clean[key] = float(clean[key]) if clean.get(key) else None
            except (TypeError, ValueError):
                clean[key] = None
        clean["id"] = str(clean.get("id") or stable_id(clean["name"], host, clean["port"]))
        return cls(**clean)

    def to_dict(self) -> dict:
        """The saved form: everyday fields first, empty optional ones left out."""
        data: Dict[str, object] = {
            "name": self.name, "host": self.host, "port": self.port, "user": self.user,
            "tags": self.tags, "forward_agent": self.forward_agent,
        }
        for key in ("identity_file", "jump_host", "extra_args"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        if self.favorite:
            data["favorite"] = True
        data["id"] = self.id
        if self.created_at:
            data["created_at"] = round(self.created_at)
        if self.last_connected:
            data["last_connected"] = round(self.last_connected)
        data["connect_count"] = self.connect_count
        return data

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_command(self) -> List[str]:
        cmd = ["ssh", "-A" if self.forward_agent else "-a", "-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", os.path.expanduser(self.identity_file)]
        if self.jump_host:
            cmd += ["-J", self.jump_host]
        if self.extra_args:
            with suppress(ValueError):
                cmd += shlex.split(self.extra_args)
        cmd.append(self.target)
        return cmd

    def shell_command(self) -> str:
        return " ".join(shlex.quote(part) for part in self.ssh_command())

    def display_command(self) -> str:
        return self.shell_command().replace(str(Path.home()) + "/", "~/")


@dataclass
class Probe:
    state: str = "unknown"  # unknown | checking | up | down | jump
    latency_ms: Optional[float] = None
    banner: str = ""
    error: str = ""
    checked_at: Optional[float] = None


FILE_NOTE = (
    "SSHStation servers. To add one by hand, copy an entry in the servers list and change it. "
    "Only host is required; name, port, user, tags, forward_agent, identity_file, jump_host, "
    "extra_args and favorite are optional. SSHStation loads your changes as soon as you save."
)


class Store:
    """Servers and settings, kept in one JSON file that is safe to edit by hand."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.servers: List[Server] = []
        self.settings: Dict[str, object] = dict(DEFAULT_SETTINGS)
        self.warning = ""  # something was skipped, but the file is usable
        self.error = ""  # the file can't be read; saving is paused so nothing is overwritten
        self.mtime: Optional[float] = None

    def _stat(self) -> Optional[float]:
        try:
            return self.path.stat().st_mtime if self.path else None
        except OSError:
            return None

    def _parse(self) -> Tuple[Optional[List[Server]], Dict[str, object], str, str]:
        """Read the file. Returns (servers or None on error, settings, warning, error)."""
        name = self.path.name if self.path else "servers.json"
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, {}, "", f"Couldn't read {name}: {exc.strerror or exc}."
        if not text.strip():
            return [], {}, "", ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, {}, "", f"{name} has a mistake on line {exc.lineno}, column {exc.colno}: {exc.msg}."
        if isinstance(data, list):
            data = {"servers": data}
        if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
            return None, {}, "", f"{name} should hold a \"servers\" list."
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        servers: List[Server] = []
        problems: List[str] = []
        seen_ids = set()
        for index, item in enumerate(data.get("servers") or [], start=1):
            try:
                server = Server.from_dict(item)
            except (ValueError, TypeError) as exc:
                label = item.get("name") if isinstance(item, dict) and item.get("name") else f"entry {index}"
                problems.append(f"{label} ({exc})")
                continue
            while server.id in seen_ids:
                server.id = stable_id(server.id, str(index), len(seen_ids))
            seen_ids.add(server.id)
            servers.append(server)
        warning = ""
        if problems:
            warning = f"Skipped {len(problems)} server{'s' if len(problems) != 1 else ''} in {name}: " + "; ".join(problems[:3])
            if len(problems) > 3:
                warning += f"; and {len(problems) - 3} more"
            warning += "."
        return servers, settings, warning, ""

    def load(self) -> "Store":
        if self.path is None or not self.path.exists():
            return self
        servers, settings, self.warning, self.error = self._parse()
        if servers is not None:
            self.servers = servers
            self.settings = {**DEFAULT_SETTINGS, **settings}
        self.mtime = self._stat()
        return self

    def changed_on_disk(self) -> bool:
        return self.path is not None and self._stat() != self.mtime

    def reload(self) -> bool:
        """Re-read after an outside edit. Returns True if the servers were replaced."""
        self.mtime = self._stat()
        if self.mtime is None:
            return False
        servers, settings, warning, error = self._parse()
        self.warning, self.error = warning, error
        if servers is None:
            return False
        self.servers = servers
        self.settings = {**DEFAULT_SETTINGS, **settings}
        return True

    def save(self) -> bool:
        if self.path is None:
            return True
        if self.error:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        payload = {
            "_about": FILE_NOTE,
            "servers": [s.to_dict() for s in self.servers],
            "settings": self.settings,
            "version": 2,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # Keep short lists such as tags on one line so entries stay easy to read.
        text = re.sub(r"\[\n\s+([^\[\]{}]*?)\n\s+\]",
                      lambda m: "[" + " ".join(line.strip() for line in m.group(1).splitlines()) + "]", text)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self.mtime = self._stat()
        return True

    def get(self, server_id: Optional[str]) -> Optional[Server]:
        return next((s for s in self.servers if s.id == server_id), None)

    def find(self, key: str) -> Optional[Server]:
        return self.get(key) or next((s for s in self.servers if s.name == key), None)

    def upsert(self, server: Server) -> None:
        for index, existing in enumerate(self.servers):
            if existing.id == server.id:
                self.servers[index] = server
                return
        self.servers.append(server)

    def remove(self, server_id: str) -> None:
        self.servers = [s for s in self.servers if s.id != server_id]

    def name_taken(self, name: str, exclude_id: Optional[str] = None) -> bool:
        return any(s.name.lower() == name.lower() and s.id != exclude_id for s in self.servers)

    def unique_name(self, base: str) -> str:
        name, n = base, 2
        while self.name_taken(name):
            name, n = f"{base}-{n}", n + 1
        return name

    def tag_counts(self) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        for server in self.servers:
            for tag in server.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items())


def config_base() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()


def default_store_path() -> Path:
    if os.environ.get("SSHSTATION_CONFIG_DIR"):
        return Path(os.environ["SSHSTATION_CONFIG_DIR"]).expanduser() / "servers.json"
    return config_base() / "sshstation" / "servers.json"


PREVIOUS_NAMES = [("SSHMan", "sshman"), ("hop", "hop")]  # earlier names of this app, newest first


def migrate_previous(path: Path) -> Tuple[int, str]:
    """On first run, copy servers from an earlier version (SSHMan or hop). Returns (count, old name)."""
    if os.environ.get("SSHSTATION_CONFIG_DIR") or path.exists():
        return 0, ""
    for label, folder in PREVIOUS_NAMES:
        legacy = config_base() / folder / "servers.json"
        if not legacy.is_file():
            continue
        old = Store(legacy).load()
        if old.error:
            continue
        new = Store(path)
        new.servers, new.settings = old.servers, old.settings
        new.save()
        return len(new.servers), label
    return 0, ""


def demo_store() -> Store:
    store = Store(None)
    now = time.time()
    samples = [
        dict(name="github", host="github.com", user="git", tags=["git"], favorite=True,
             last_connected=now - 420, connect_count=58),
        dict(name="gitlab", host="gitlab.com", user="git", tags=["git"],
             last_connected=now - 3 * 86400, connect_count=12),
        dict(name="web-01", host="web-01.prod.example.com", user="deploy", tags=["prod", "web"],
             forward_agent=True, last_connected=now - 5 * 3600, connect_count=31),
        dict(name="db-primary", host="10.255.255.1", user="postgres", tags=["prod", "db"],
             identity_file="~/.ssh/id_ed25519", connect_count=9, last_connected=now - 9 * 86400),
        dict(name="api-staging", host="api.staging.example.com", port=2222, user="ubuntu",
             tags=["staging", "api"], last_connected=now - 26 * 3600, connect_count=4),
        dict(name="worker-02", host="10.0.4.12", user="deploy", tags=["prod", "jobs"],
             jump_host="deploy@bastion.example.com", connect_count=2),
        dict(name="homelab", host="192.168.1.40", user="pi", tags=["home"], connect_count=0),
        dict(name="sandbox", host="bitbucket.org", user="git", tags=["dev"],
             last_connected=now - 40 * 86400, connect_count=1),
    ]
    store.servers = [Server.from_dict(s) for s in samples]
    return store


# --------------------------------------------------------------------------- ~/.ssh/config


def parse_ssh_config(path: Optional[Path] = None) -> List[dict]:
    """Read concrete Host entries (no wildcards) from an OpenSSH client config."""
    root = Path(path or "~/.ssh/config").expanduser()
    ssh_dir = Path("~/.ssh").expanduser()
    entries: List[dict] = []
    visited = set()

    def read(file: Path, depth: int) -> None:
        if depth > 8 or file in visited or not file.is_file():
            return
        visited.add(file)
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        current: Optional[List[dict]] = None
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"(\S+?)(?:\s*=\s*|\s+)(.*)$", line)
            if not match:
                continue
            key, value = match.group(1).lower(), match.group(2).strip()
            if key == "include":
                for pattern in shlex.split(value):
                    pattern = os.path.expanduser(pattern)
                    if not os.path.isabs(pattern):
                        pattern = str(ssh_dir / pattern)
                    for found in sorted(glob.glob(pattern)):
                        read(Path(found), depth + 1)
                continue
            if key == "host":
                aliases = [a for a in value.split() if not any(c in a for c in "*?!")]
                current = [{"alias": a} for a in aliases]
                entries.extend(current)
                continue
            if key == "match":
                current = None
                continue
            if current is None:
                continue
            value = value.strip('"')
            for entry in current:
                entry.setdefault(key, value)  # first value wins, as in ssh

    read(root, 0)

    result: List[dict] = []
    for entry in entries:
        alias = entry["alias"]
        hostname = entry.get("hostname", alias).replace("%h", alias)
        try:
            port = int(entry.get("port", 22))
        except ValueError:
            port = 22
        result.append(
            dict(
                name=alias,
                host=hostname,
                port=port,
                user=entry.get("user", ""),
                forward_agent=entry.get("forwardagent", "no").lower() in ("yes", "true"),
                identity_file=entry.get("identityfile", ""),
                jump_host=entry.get("proxyjump", "") if entry.get("proxyjump", "").lower() != "none" else "",
            )
        )
    return result


# --------------------------------------------------------------------------- probing


async def probe(host: str, port: int, timeout: float) -> Probe:
    """Open a TCP connection, read the SSH banner, then hang up politely."""
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return Probe("down", error="timeout", checked_at=time.time())
    except socket.gaierror:
        return Probe("down", error="no dns", checked_at=time.time())
    except ConnectionRefusedError:
        return Probe("down", error="refused", checked_at=time.time())
    except OSError as exc:
        if isinstance(exc, TimeoutError):
            reason = "timeout"
        elif exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            reason = "unreachable"
        else:
            reason = "error"
        return Probe("down", error=reason, checked_at=time.time())
    latency = (time.perf_counter() - started) * 1000
    banner = ""
    try:
        line = await asyncio.wait_for(reader.readline(), min(1.5, timeout))
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("SSH-"):
            banner = text
            # Identify ourselves so sshd logs a clean disconnect, not a scan.
            writer.write(b"SSH-2.0-SSHStation_probe\r\n")
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        with suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 1.0)
    return Probe("up", latency_ms=latency, banner=banner, checked_at=time.time())


# --------------------------------------------------------------------------- helpers


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{max(1, int(delta // 60))}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    when = datetime.fromtimestamp(ts)
    return when.strftime("%b %d") if when.year == datetime.now().year else when.strftime("%b %Y")


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def is_iterm() -> bool:
    in_iterm = os.environ.get("TERM_PROGRAM") == "iTerm.app" or "ITERM_SESSION_ID" in os.environ
    return in_iterm and "TMUX" not in os.environ


def copy_text(text: str) -> bool:
    if shutil.which("pbcopy"):
        with suppress(Exception):
            subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=3)
            return True
    return False


def tab_backend() -> Optional[str]:
    """Where new tabs can open: inside tmux, in iTerm2, or nowhere."""
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return "tmux"
    if os.environ.get("TERM_PROGRAM") == "iTerm.app" or "ITERM_SESSION_ID" in os.environ:
        return "iterm" if shutil.which("osascript") else None
    return None


def self_command() -> List[str]:
    """How to start this program again, for the command typed into a new tab."""
    launcher = os.environ.get("SSHSTATION_LAUNCHER")
    if launcher and os.path.isfile(launcher):
        return [launcher]
    argv0 = os.path.abspath(sys.argv[0])
    if argv0.endswith(".py"):
        return [sys.executable, argv0]
    if os.path.isfile(argv0) and os.access(argv0, os.X_OK):
        return [argv0]
    return [sys.executable, os.path.abspath(__file__)]


def tab_command(server: Server, settings: dict) -> str:
    parts = self_command() + ["--open", server.id]
    command = " ".join(shlex.quote(p) for p in parts)
    if os.environ.get("SSHSTATION_CONFIG_DIR"):
        command = f"env SSHSTATION_CONFIG_DIR={shlex.quote(os.environ['SSHSTATION_CONFIG_DIR'])} {command}"
    return f"exec {command}" if settings.get("close_tab_on_exit", True) else command


def open_in_tabs(servers: List[Server], settings: dict) -> Optional[str]:
    """Open each server in its own new tab. Returns an error message on failure."""
    backend = tab_backend()
    if backend == "tmux":
        for server in servers:
            result = subprocess.run(["tmux", "new-window", "-n", server.name, tab_command(server, settings)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                return (result.stderr or "tmux refused to open a window.").strip()
        return None
    if backend != "iterm":
        return "New tabs need iTerm2 or tmux."

    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    blocks = []
    for server in servers:
        # The leading space keeps the line out of zsh history; clear hides it from view.
        typed = f" clear; {tab_command(server, settings)}"
        blocks.append(f'''
        set newTab to (create tab with default profile)
        tell current session of newTab
            set name to "{quote(server.name)}"
            write text "{quote(typed)}"
        end tell''')
    script = f'''
tell application "iTerm2"
    tell current window{"".join(blocks)}
    end tell
end tell
'''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "iTerm2 didn't respond. Check System Settings > Privacy & Security > Automation."
    if result.returncode != 0:
        message = (result.stderr or "iTerm2 refused the request.").strip().splitlines()[-1]
        if "-1743" in message or "Not authorized" in message:
            return "Allow your terminal to control iTerm2 in System Settings > Privacy & Security > Automation."
        return message
    return None


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _fg(hex_color: str) -> str:
    r, g, b = _rgb(hex_color)
    return f"\033[38;2;{r};{g};{b}m"


def run_session(server: Server, settings: dict, pause_on_error: bool = True) -> Tuple[int, float]:
    """Hand the terminal to ssh. Returns (exit code, seconds connected)."""
    out = sys.__stdout__
    brass, teal, muted, red, reset, bold = _fg("#E8B85C"), _fg("#5EC4B6"), _fg("#7D8AA3"), _fg("#FF5A5F"), "\033[0m", "\033[1m"
    details = [f"{server.target}", f"port {server.port}",
               f"agent forwarding {'on' if server.forward_agent else 'off'}"]
    if server.jump_host:
        details.append(f"via {server.jump_host}")
    out.write(f"\n  {brass}{bold}◆ {APP_NAME}{reset}  {muted}connecting to{reset} {teal}{bold}{server.name}{reset}\n")
    out.write(f"  {muted}{'   '.join(details)}{reset}\n\n")

    iterm = is_iterm()
    tint = None
    if iterm:
        out.write(f"\033]1;{server.name}\007")
        if settings.get("iterm_badge"):
            badge = base64.b64encode(server.name.encode()).decode()
            out.write(f"\033]1337;SetBadgeFormat={badge}\007")
        if settings.get("iterm_tab_color"):
            tint = next((TAB_COLORS[tag_color(t)] for t in server.tags if t in ENV_TAGS), None)
            if tint:
                for channel, value in zip(("red", "green", "blue"), tint):
                    out.write(f"\033]6;1;bg;{channel};brightness;{value}\007")
    out.flush()

    started = time.time()
    try:
        code = subprocess.call(server.ssh_command())
    except KeyboardInterrupt:
        code = 130
    except FileNotFoundError:
        out.write(f"  {red}ssh wasn't found on your PATH.{reset}\n")
        code = 127
    elapsed = time.time() - started

    if iterm:
        if settings.get("iterm_badge"):
            out.write("\033]1337;SetBadgeFormat=\007")
        if tint:
            out.write("\033]6;1;bg;*;default\007")
        out.write(f"\033]1;{PROG}\007")
        out.flush()

    if pause_on_error and code not in (0, 130) and elapsed < 5:
        out.write(f"\n  {red}ssh exited with code {code}.{reset} {muted}Press Enter to return to {PROG}.{reset}")
        out.flush()
        with suppress(EOFError, KeyboardInterrupt):
            input()
    return code, elapsed


# --------------------------------------------------------------------------- widgets


class ServerTable(DataTable):
    BINDINGS = [
        Binding("enter", "select_cursor", "Open", key_display="⏎"),
        Binding("space", "app.toggle_mark", "Select", key_display="space"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "First", show=False),
        Binding("G", "scroll_bottom", "Last", show=False),
    ]


class SearchInput(Input):
    BINDINGS = [
        Binding("down", "app.focus_table", "Results", show=False),
        Binding("escape", "app.clear_search", "Clear", show=False),
    ]


def keycap(key: str, accent: str, ink: str) -> Text:
    return Text(f" {key} ", style=f"bold {ink} on {accent}")


# --------------------------------------------------------------------------- dialogs


class ServerForm(ModalScreen[Optional[Server]]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, store: Store, server: Optional[Server] = None, *, editing: bool = False, title: str = "Add server"):
        super().__init__()
        self.store = store
        self.server = server
        self.editing = editing
        self.dialog_title = title

    def compose(self) -> ComposeResult:
        s = self.server
        local_user = getpass.getuser()

        def field_(label: str, widget: Input) -> Vertical:
            return Vertical(Label(label), widget, classes="field")

        with Vertical(id="dialog"):
            fields_ = [
                ("Name", Input(s.name if s else "", placeholder="web-01", id="f-name")),
                ("Tags", Input(", ".join(s.tags) if s else "", placeholder="prod, web", id="f-tags")),
                ("Host", Input(s.host if s else "", placeholder="10.0.1.15 or user@host:port", id="f-host")),
                ("Port", Input(str(s.port) if s else "22", placeholder="22", type="integer", max_length=5, id="f-port")),
                ("Username", Input(s.user if s else "", placeholder=f"{local_user} (your local name)", id="f-user")),
                ("Identity file", Input(s.identity_file if s else "", placeholder="~/.ssh/id_ed25519 (optional)", id="f-identity")),
                ("Jump host", Input(s.jump_host if s else "", placeholder="user@bastion (optional)", id="f-jump")),
                ("Extra ssh options", Input(s.extra_args if s else "", placeholder="-L 8080:localhost:80 (optional)", id="f-extra")),
            ]
            for left, right in zip(fields_[0::2], fields_[1::2]):
                with Horizontal(classes="form-row"):
                    yield field_(*left)
                    yield field_(*right)
            with Horizontal(id="agent-row"):
                yield Switch(value=bool(s and s.forward_agent), id="f-agent")
                with Vertical(id="agent-text"):
                    yield Label("Forward SSH agent", id="agent-label")
                    yield Static("Lets this server use your local keys (ssh -A). Turn on only for hosts you trust.", id="agent-hint")
            yield Static("", id="form-error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save server" if not self.editing else "Save changes", id="save", variant="primary")

    def on_mount(self) -> None:
        dialog = self.query_one("#dialog")
        dialog.border_title = self.dialog_title
        dialog.border_subtitle = "ctrl+s save   esc cancel"
        self.query_one("#f-name", Input).focus()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save_pressed(self) -> None:
        self.action_save()

    def _fail(self, message: str, field_id: str) -> None:
        error = self.query_one("#form-error", Static)
        error.update(Text(f"✕ {message}"))
        error.add_class("-visible")
        widget = self.query_one(f"#{field_id}", Input)
        widget.add_class("-error")
        widget.focus()

    def action_save(self) -> None:
        for widget in self.query(".field Input"):
            widget.remove_class("-error")
        value = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        name, tags, host_raw = value("f-name"), value("f-tags"), value("f-host")
        port_raw, user, identity = value("f-port"), value("f-user"), value("f-identity")
        jump, extra = value("f-jump"), value("f-extra")

        parsed_user, host, parsed_port = split_target(host_raw)
        user = user or parsed_user
        if parsed_port and port_raw in ("", "22"):
            port_raw = str(parsed_port)

        if not host:
            return self._fail("Enter a host name or IP address.", "f-host")
        if re.search(r"\s", host) or "@" in host:
            return self._fail("The host can't contain spaces or a second @.", "f-host")
        try:
            port = int(port_raw or "22")
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            return self._fail("Port must be a number from 1 to 65535.", "f-port")
        name = name or host
        own_id = self.server.id if (self.server and self.editing) else None
        if self.store.name_taken(name, own_id):
            return self._fail(f"Another server is already called “{name}”.", "f-name")
        if identity and not Path(identity).expanduser().is_file():
            return self._fail(f"No file at {identity}. Leave it blank to use your default keys.", "f-identity")
        try:
            shlex.split(extra)
        except ValueError:
            return self._fail("The extra options have an unclosed quote.", "f-extra")

        values = dict(
            name=name, host=host, port=port, user=user, tags=normalize_tags(tags),
            identity_file=identity, jump_host=jump, extra_args=extra,
            forward_agent=self.query_one("#f-agent", Switch).value,
        )
        if self.server and self.editing:
            self.dismiss(replace(self.server, **values))
        else:
            self.dismiss(Server(**values, favorite=bool(self.server and self.server.favorite)))


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "cancel", "Cancel"),
        Binding("y", "confirm", "Confirm"),
    ]

    def __init__(self, title: str, message: str, confirm_label: str, variant: str = "error"):
        super().__init__()
        self.title_text, self.message, self.confirm_label = title, message, confirm_label
        self.variant = variant

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(Text(self.title_text, style="bold"), id="confirm-title")
            yield Static(self.message, id="confirm-message")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant=self.variant)

    def on_mount(self) -> None:
        if self.variant != "error":
            self.query_one("#confirm-dialog").add_class("-primary")
            self.query_one("#confirm", Button).focus()
        else:
            self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def action_confirm(self) -> None:
        self.dismiss(True)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    SECTIONS = [
        ("Connect", [("⏎", "Open in a new tab (every selected server, if any)"),
                     ("space", "Select or unselect, to open several at once"),
                     ("ctrl+a", "Select all shown"),
                     ("o", "Connect here, in this window"),
                     ("c", "Copy the ssh command")]),
        ("Manage", [("a", "Add a server"), ("e", "Edit"), ("d", "Duplicate"),
                    ("x", "Delete"), ("f", "Add to or remove from favorites"),
                    ("i", "Import hosts from ~/.ssh/config"),
                    ("ctrl+o", "Edit the servers file in your editor")]),
        ("Find", [("/", "Search by name, host, user or tag"), ("esc", "Clear the search, then the selection"),
                  ("s", "Change sort order"), ("j k", "Move down and up"),
                  ("g G", "Jump to the first or last server")]),
        ("View", [("r", "Check which servers are reachable"), ("ctrl+b", "Show or hide tags"),
                  ("ctrl+p", "Command palette, including themes"), ("q", "Quit")]),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static(id="help-body")

    def on_mount(self) -> None:
        dialog = self.query_one("#help-dialog")
        dialog.border_title = "Keyboard shortcuts"
        dialog.border_subtitle = "esc close"
        accent = self.app.get_css_variables().get("accent", "#E8B85C")[:7]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="right", no_wrap=True, width=8)
        grid.add_column()
        for index, (section, rows) in enumerate(self.SECTIONS):
            if index:
                grid.add_row("", "")
            grid.add_row("", Text(section, style="bold"))
            for key, text in rows:
                grid.add_row(Text(key, style=f"bold {accent}"), Text(text))
        grid.add_row("", "")
        credit = Text(f"{APP_NAME} {__version__}   ", style="dim")
        credit.append("An Idea by ", style="dim")
        credit.append(AUTHOR, style=f"bold {accent}")
        grid.add_row("", credit)
        self.query_one("#help-body", Static).update(grid)

    def action_close(self) -> None:
        self.dismiss(None)


class ImportScreen(ModalScreen[Optional[List[dict]]]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "import", "Import")]

    def __init__(self, entries: List[dict], store: Store):
        super().__init__()
        self.entries = entries
        self.store = store

    def compose(self) -> ComposeResult:
        selections = []
        for index, entry in enumerate(self.entries):
            exists = self.store.name_taken(entry["name"])
            label = Text(entry["name"], style="bold")
            target = f"{entry['user'] + '@' if entry['user'] else ''}{entry['host']}:{entry['port']}"
            label.append(f"   {target}", style="dim")
            if exists:
                label.append("   already added", style="dim italic")
            selections.append(Selection(label, index, initial_state=not exists, disabled=exists))
        with Vertical(id="import-dialog"):
            yield Static("Pick the hosts to add. Space toggles a host.", id="import-intro")
            yield SelectionList(*selections, id="import-list")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Import", id="import", variant="primary")

    def on_mount(self) -> None:
        dialog = self.query_one("#import-dialog")
        dialog.border_title = "Import from ~/.ssh/config"
        dialog.border_subtitle = "space toggle   ctrl+s import   esc cancel"
        self.query_one(SelectionList).focus()
        self._update_button()

    @on(SelectionList.SelectedChanged)
    def _update_button(self) -> None:
        count = len(self.query_one(SelectionList).selected)
        button = self.query_one("#import", Button)
        button.label = f"Import {count} server{'s' if count != 1 else ''}"
        button.disabled = count == 0

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#import")
    def action_import(self) -> None:
        chosen = [self.entries[i] for i in self.query_one(SelectionList).selected]
        if chosen:
            self.dismiss(chosen)


# --------------------------------------------------------------------------- app

CSS = """
Screen { background: $background; }

#topbar { height: 1; background: $panel; padding: 0 1; }
#brand { width: 1fr; }
#clock { width: auto; color: $text-muted; }
#author { width: auto; margin-left: 3; }

#body { height: 1fr; }

#sidebar { width: 28; background: $surface; padding: 1 1 0 1; }
#sidebar .section-title { color: $text-muted; padding: 0 1; height: 1; margin-bottom: 1; }
#tags { height: 1fr; background: transparent; border: none; padding: 0; scrollbar-size-vertical: 1; }
#tags:focus { background-tint: $foreground 3%; }
#tags > .option-list--option { padding: 0 1; }
#tags > .option-list--option-highlighted { background: $accent 18%; color: $foreground; text-style: none; }
#tags:focus > .option-list--option-highlighted { background: $accent 30%; text-style: none; }
#tags > .option-list--option-hover { background: $boost; }
#legend { height: auto; padding: 1 1; color: $text-muted; }

#main { width: 1fr; padding: 0 1 0 2; }

#search {
    margin: 1 0 0 0;
    border: round $panel-lighten-2;
    background: $background;
    padding: 0 1;
}
#search:focus { border: round $accent; background-tint: $foreground 0%; }

ServerTable {
    height: 1fr;
    margin-top: 1;
    background: $background;
    scrollbar-size-vertical: 1;
}
ServerTable:focus { background-tint: $foreground 0%; }
ServerTable > .datatable--header { background: $background; color: $text-muted; text-style: bold; }
ServerTable:focus > .datatable--header { background-tint: $foreground 0%; }
ServerTable > .datatable--even-row { background: $surface 55%; }
ServerTable > .datatable--cursor { background: $accent 14%; color: $foreground; text-style: none; }
ServerTable:focus > .datatable--cursor { background: $accent 26%; color: $foreground; text-style: none; }
ServerTable > .datatable--hover { background: $boost; }
ServerTable > .datatable--header-hover { background: $background; }

#empty { height: 1fr; content-align: center middle; text-align: center; display: none; }

#details {
    height: 10;
    margin: 1 0 1 0;
    padding: 0 1;
    background: $surface;
    border: round $panel-lighten-2;
    border-title-color: $accent;
    border-title-style: bold;
    border-subtitle-color: $text-muted;
}

#statusbar { height: 1; background: $panel; padding: 0 1; }
#status-left { width: 1fr; }
#status-right { width: auto; color: $text-muted; }

Footer { background: $surface; }

/* dialogs */
ModalScreen { align: center middle; background: $background 65%; }

#dialog, #confirm-dialog, #help-dialog, #import-dialog {
    background: $surface;
    border: round $accent;
    border-title-color: $accent;
    border-title-style: bold;
    border-subtitle-color: $text-muted;
    padding: 1 2;
    height: auto;
    max-height: 96%;
    overflow-y: auto;
}
#dialog { width: 92; max-width: 96%; }
.form-row { height: auto; }
.field { height: auto; width: 1fr; margin-bottom: 1; }
.form-row .field:first-child { margin-right: 3; }
.field Label { color: $text-muted; height: 1; }
.field Input {
    height: 1;
    border: none;
    border-left: outer $panel-lighten-2;
    padding: 0 1;
    background: $panel;
}
.field Input:focus { border: none; border-left: outer $accent; background: $panel-lighten-1; }
.field Input.-error { border-left: outer $error; }

#agent-row { height: auto; margin-top: 0; }
#agent-row Switch { margin-right: 2; }
#agent-row Switch.-on > .switch--slider { color: $warning; }
#agent-text { height: auto; width: 1fr; }
#agent-label { height: 1; }
#agent-hint { color: $text-muted; height: auto; }
#form-error { color: $error; height: auto; margin-top: 1; display: none; }
#form-error.-visible { display: block; }

.buttons { height: auto; align-horizontal: right; margin-top: 1; }
.buttons Button { margin-left: 2; min-width: 14; height: 1; border: none; padding: 0 2; background: $panel-lighten-1; color: $foreground; text-style: bold; }
.buttons Button:focus { background: $panel-lighten-2; text-style: bold reverse; }
.buttons Button.-primary { background: $accent; color: $background; }
.buttons Button.-primary:focus { background: $accent-lighten-1; text-style: bold; }
.buttons Button.-error { background: $error; color: $background; }
.buttons Button.-error:focus { background: $error-lighten-1; text-style: bold; }
.buttons Button:disabled { background: $panel; color: $text-muted; }

#confirm-dialog { width: 62; max-width: 96%; border: round $error; }
#confirm-dialog.-primary { border: round $accent; }
#confirm-message { color: $text-muted; margin-top: 1; }

#help-dialog { width: 72; max-width: 96%; }

#import-dialog { width: 86; max-width: 96%; }
#import-intro { color: $text-muted; margin-bottom: 1; }
#import-list { height: auto; max-height: 20; background: $background; border: round $panel-lighten-2; }
#import-list:focus { border: round $accent; }
"""

OUR_ACTIONS = {
    "add", "edit", "duplicate", "delete", "favorite", "copy", "open", "connect_here",
    "toggle_mark", "mark_all", "edit_file", "search", "recheck", "cycle_sort",
    "import_config", "toggle_sidebar", "help", "quit",
}
NEEDS_SERVER = {"edit", "duplicate", "delete", "favorite", "copy", "open", "connect_here", "toggle_mark"}


class SSHStationApp(App):
    TITLE = APP_NAME
    CSS = CSS
    BINDINGS = [
        Binding("o", "connect_here", "Open here"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("slash", "search", "Search", key_display="/"),
        Binding("r", "recheck", "Recheck"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
        Binding("d", "duplicate", "Duplicate", show=False),
        Binding("x,delete", "delete", "Delete", show=False),
        Binding("f", "favorite", "Favorite", show=False),
        Binding("c", "copy", "Copy command", show=False),
        Binding("s", "cycle_sort", "Sort", show=False),
        Binding("i", "import_config", "Import", show=False),
        Binding("ctrl+a", "mark_all", "Select all", show=False),
        Binding("ctrl+o", "edit_file", "Edit servers file", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Tags", show=False),
        Binding("escape", "clear_search", "Clear", show=False),
    ]

    def __init__(self, store: Store, initial_search: str = "", demo: bool = False,
                 migrated: Tuple[int, str] = (0, "")):
        super().__init__()
        self.store = store
        self.demo = demo
        self.migrated = migrated
        self.probes: Dict[str, Probe] = {}
        self.tag_filter: Optional[str] = None
        self.search = initial_search
        sort = str(store.settings.get("sort", "name"))
        self.sort_mode = sort if sort in dict(SORTS) else "name"
        self.last_check: Optional[float] = None
        self.marked: set = set()
        self.backend = tab_backend()
        self._error_shown = ""
        self.row_ids: List[str] = []
        self.pal: Dict[str, str] = dict(PALETTE_DEFAULTS)
        self._spin = 0
        self._ticks = 0
        self._narrow = False
        self._width = 200
        self._column_keys: List[str] = []

    # ---- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="brand")
            yield Static(id="clock")
            yield Static(id="author")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("Tags", classes="section-title")
                yield OptionList(id="tags")
                yield Static(id="legend")
            with Vertical(id="main"):
                yield SearchInput(self.search, placeholder="Search by name, host, user or tag", id="search")
                yield ServerTable(id="table", cursor_type="row", zebra_stripes=True,
                                  cursor_foreground_priority="renderable")
                yield Static(id="empty")
                with Vertical(id="details"):
                    yield Static(id="details-body")
        with Horizontal(id="statusbar"):
            yield Static(id="status-left")
            yield Static(id="status-right")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(HARBOR)
        wanted = str(self.store.settings.get("theme") or "harbor")
        self.theme = wanted if wanted in self.available_themes else "harbor"
        self._load_palette()
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self._apply_layout(self.size.width, self.size.height)
        self._render_chrome()
        self.rebuild()
        self.query_one(ServerTable).focus()
        self.set_interval(0.1, self._spin_tick)
        self.set_interval(1.0, self._clock_tick)
        interval = float(self.store.settings.get("refresh_interval") or 0)
        if interval >= 10:
            self.set_interval(interval, self.check_all)
        self.check_all()
        if self.store.warning:
            self.notify(self.store.warning, severity="warning", timeout=12)
        self._report_file_error()
        count, previous = self.migrated
        if count:
            self.notify(f"Brought over {count} server{'s' if count != 1 else ''} from {previous}.",
                        title=f"Welcome to {APP_NAME}", timeout=8)
        if self.demo:
            self.notify("Demo mode. Changes you make here aren't saved.", title="Sample servers", timeout=6)

    def on_resize(self, event) -> None:
        self._apply_layout(event.size.width, event.size.height)

    def _apply_layout(self, width: int, height: int) -> None:
        self._narrow = width < 100
        sidebar = self.query_one("#sidebar")
        sidebar.display = bool(self.store.settings.get("show_sidebar", True)) and not self._narrow
        table_width = width - (28 if sidebar.display else 0) - 3 - 1
        changed = table_width != self._width
        self._width = table_width
        if changed and self._column_keys:
            self._rebuild_table()
        self._update_empty_state()
        self._update_statusbar()

    # ---- theme

    def _load_palette(self) -> None:
        css = self.get_css_variables()
        fallbacks = PALETTE_DEFAULTS

        def norm(value: Optional[str], fallback: str) -> str:
            if not value:
                return fallback
            value = value.strip()
            if value.startswith("ansi_"):
                return value[5:]
            if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
                return value[:7]
            return fallback

        self.pal = {key: norm(css.get(key), fb) for key, fb in fallbacks.items()}

    def _theme_changed(self, theme: Theme) -> None:
        self._load_palette()
        if not self.demo:
            self.store.settings["theme"] = theme.name
            self._save()
        self._render_chrome()
        self.rebuild()

    def _render_chrome(self) -> None:
        brand = Text()
        brand.append(f"◆ {APP_NAME}", style=f"bold {self.pal['accent']}")
        if self.demo:
            brand.append("   demo", style=f"italic {self.pal['secondary']}")
        self.query_one("#brand", Static).update(brand)
        author = Text("An Idea by ", style="dim")
        author.append(AUTHOR, style=f"bold {self.pal['accent']}")
        self.query_one("#author", Static).update(author)
        legend = Text()
        legend.append("● ", style=self.pal["success"])
        legend.append("reachable\n")
        legend.append("● ", style=self.pal["error"])
        legend.append("unreachable\n")
        legend.append("◇ ", style=self.pal["secondary"])
        legend.append("behind a jump host")
        self.query_one("#legend", Static).update(legend)
        self._clock_tick()

    # ---- data views

    def selected_server(self) -> Optional[Server]:
        try:
            row = self.query_one(ServerTable).cursor_row
        except Exception:
            return None
        if 0 <= row < len(self.row_ids):
            return self.store.get(self.row_ids[row])
        return None

    def probe_for(self, server: Server) -> Probe:
        if server.jump_host:
            return self.probes.get(server.id) or Probe("jump")
        return self.probes.get(server.id) or Probe("unknown")

    def visible_servers(self) -> List[Server]:
        items = list(self.store.servers)
        if self.tag_filter == FAVORITES:
            items = [s for s in items if s.favorite]
        elif self.tag_filter:
            items = [s for s in items if self.tag_filter in s.tags]
        tokens = self.search.lower().split()
        if tokens:
            def haystack(s: Server) -> str:
                return " ".join([s.name, s.host, s.user, s.jump_host, *s.tags]).lower()
            items = [s for s in items if all(t in haystack(s) for t in tokens)]

        rank = {"up": 0, "jump": 1, "checking": 2, "unknown": 3, "down": 4}
        if self.sort_mode == "recent":
            key = lambda s: (not s.favorite, -(s.last_connected or 0), s.name.lower())  # noqa: E731
        elif self.sort_mode == "status":
            def key(s: Server):
                p = self.probe_for(s)
                return (not s.favorite, rank.get(p.state, 9), p.latency_ms or 0, s.name.lower())
        elif self.sort_mode == "usage":
            key = lambda s: (not s.favorite, -s.connect_count, s.name.lower())  # noqa: E731
        else:
            key = lambda s: (not s.favorite, s.name.lower())  # noqa: E731
        return sorted(items, key=key)

    # ---- cells

    def _status_cell(self, s: Server) -> Text:
        p = self.probe_for(s)
        if p.state == "up":
            return Text("●", style=self.pal["success"])
        if p.state == "down":
            return Text("●", style=self.pal["error"])
        if p.state == "checking":
            return Text(SPINNER[self._spin % len(SPINNER)], style=self.pal["warning"])
        if p.state == "jump":
            return Text("◇", style=self.pal["secondary"])
        return Text("○", style="dim")

    def _ping_cell(self, s: Server) -> Text:
        p = self.probe_for(s)
        if p.state == "up" and p.latency_ms is not None:
            ms = p.latency_ms
            label = "<1 ms" if ms < 1 else f"{ms:.0f} ms"
            return Text(label, style=self.pal["success"] if ms < 150 else self.pal["warning"])
        if p.state == "down":
            return Text(p.error or "offline", style=self.pal["error"])
        if p.state == "checking":
            return Text("checking", style="dim")
        if p.state == "jump":
            return Text("via jump", style=f"{self.pal['secondary']}")
        return Text("—", style="dim")

    def _name_cell(self, s: Server) -> Text:
        text = Text()
        text.append("★ " if s.favorite else "  ", style=self.pal["accent"])
        text.append(s.name, style=f"bold {self.pal['accent']}" if s.id in self.marked else "bold")
        return text

    def _mark_cell(self, s: Server) -> Text:
        return Text("✓", style=f"bold {self.pal['accent']}") if s.id in self.marked else Text(" ")

    def _tags_cell(self, s: Server) -> Text:
        text = Text()
        for index, tag in enumerate(s.tags[:3]):
            if index:
                text.append(" ")
            text.append(f" {tag} ", style=f"bold #0A1220 on {tag_color(tag)}")
        if len(s.tags) > 3:
            text.append(f" +{len(s.tags) - 3}", style="dim")
        return text

    def _last_cell(self, s: Server) -> Text:
        if not s.last_connected:
            return Text("never", style="dim italic")
        recent = time.time() - s.last_connected < 86400
        return Text(ago(s.last_connected), style="" if recent else "dim")

    def _cells(self, s: Server, keys: List[str]) -> list:
        makers = {
            "mark": self._mark_cell,
            "status": self._status_cell,
            "name": self._name_cell,
            "host": lambda s: Text(s.host),
            "port": lambda s: Text(str(s.port), style="dim" if s.port == 22 else "bold", justify="right"),
            "user": lambda s: Text(s.user) if s.user else Text("—", style="dim"),
            "tags": self._tags_cell,
            "ping": self._ping_cell,
            "last": self._last_cell,
        }
        return [makers[key](s) for key in keys]

    def _columns(self) -> List[Tuple[str, Text, Optional[int]]]:
        accent = self.pal["accent"]

        def header(label: str, sort: Optional[str] = None, arrow: str = "↑") -> Text:
            text = Text(label)
            if sort and sort == self.sort_mode:
                text.append(f" {arrow}", style=f"bold {accent}")
            return text

        columns = [
            ("mark", Text(" "), 1),
            ("status", Text(" "), 1),
            ("name", header("  Name", "name"), None),
            ("host", Text("Host"), None),
            ("port", Text("Port", justify="right"), 5),
            ("user", Text("User"), None),
            ("tags", Text("Tags"), None),
            ("ping", header("Ping", "status"), 11),
            ("last", header("Last connected", "recent", "↓"), 16),
        ]
        return columns

    def _fit_columns(self, columns, servers: List[Server]):
        """Drop the least important columns until the table fits the window."""
        def needed(key: str, label: Text, width: Optional[int]) -> int:
            if width:
                return width + 2
            cells = [self._cells(s, [key])[0].cell_len for s in servers]
            return max([label.cell_len, *cells]) + 2

        widths = {key: needed(key, label, width) for key, label, width in columns}
        keep = [key for key, _, _ in columns]
        for drop in ("user", "tags", "last", "port"):
            if sum(widths[k] for k in keep) + 1 <= self._width:
                break
            keep.remove(drop)
        return [c for c in columns if c[0] in keep]

    # ---- rendering

    def rebuild(self, keep_id: Optional[str] = None) -> None:
        self._rebuild_tags()
        self._rebuild_table(keep_id)

    def _tag_prompt(self, marker: str, color: str, label: str, count: int) -> Text:
        text = Text()
        text.append(f"{marker} ", style=color)
        text.append(label[:15].ljust(15))
        text.append(f"{count:>4}", style="dim")
        return text

    def _rebuild_tags(self) -> None:
        tags = self.store.tag_counts()
        favorites = sum(1 for s in self.store.servers if s.favorite)
        options: list = [
            Option(self._tag_prompt("◆", self.pal["primary"], "All servers", len(self.store.servers)), id="all"),
            Option(self._tag_prompt("★", self.pal["accent"], "Favorites", favorites), id="fav"),
        ]
        if tags:
            options.append(None)
        for tag, count in tags:
            options.append(Option(self._tag_prompt("●", tag_color(tag), tag, count), id=f"tag:{tag}"))
        valid = {None, FAVORITES} | {t for t, _ in tags}
        if self.tag_filter not in valid:
            self.tag_filter = None
        wanted = "all" if self.tag_filter is None else "fav" if self.tag_filter == FAVORITES else f"tag:{self.tag_filter}"
        option_list = self.query_one("#tags", OptionList)
        option_list.set_options(options)
        with suppress(Exception):
            option_list.highlighted = option_list.get_option_index(wanted)

    def _rebuild_table(self, keep_id: Optional[str] = None) -> None:
        table = self.query_one(ServerTable)
        if keep_id is None:
            current = self.selected_server()
            keep_id = current.id if current else None
        table.clear(columns=True)
        servers = self.visible_servers()
        columns = self._fit_columns(self._columns(), servers)
        self._column_keys = [key for key, _, _ in columns]
        for key, label, width in columns:
            table.add_column(label, key=key, width=width)
        self.row_ids = [s.id for s in servers]
        for s in servers:
            table.add_row(*self._cells(s, self._column_keys), key=s.id)
        if keep_id in self.row_ids:
            table.move_cursor(row=self.row_ids.index(keep_id), animate=False)
        elif self.row_ids:
            table.move_cursor(row=0, animate=False)
        self._update_empty_state()
        self._update_details()
        self._update_statusbar()
        self.refresh_bindings()

    def _refresh_row(self, server_id: str, columns=("status", "ping")) -> None:
        if server_id not in self.row_ids:
            return
        server = self.store.get(server_id)
        if not server:
            return
        table = self.query_one(ServerTable)
        makers = {"status": self._status_cell, "ping": self._ping_cell, "last": self._last_cell,
                  "mark": self._mark_cell, "name": self._name_cell}
        for column in columns:
            if column not in self._column_keys:
                continue
            with suppress(Exception):
                table.update_cell(server_id, column, makers[column](server))

    def _update_empty_state(self) -> None:
        try:
            table = self.query_one(ServerTable)
            empty = self.query_one("#empty", Static)
            details = self.query_one("#details")
        except Exception:
            return
        has_rows = bool(self.row_ids)
        table.display = has_rows
        empty.display = not has_rows
        details.display = has_rows and self.size.height >= 26
        if has_rows:
            return
        accent, ink = self.pal.get("accent", "#E8B85C"), self.pal.get("background", "#0A1220")
        message = Text()
        if not self.store.servers:
            message.append("◆\n\n", style=f"bold {accent}")
            message.append("No servers yet\n\n", style="bold")
            message.append("Press ")
            message.append_text(keycap("a", accent, ink))
            message.append(" to add one, or ")
            message.append_text(keycap("i", accent, ink))
            message.append(" to import hosts from ~/.ssh/config.", style="")
        elif self.search.strip():
            message.append(f"Nothing matches “{self.search.strip()}”\n\n", style="bold")
            message.append("Press ")
            message.append_text(keycap("esc", accent, ink))
            message.append(" to clear the search.")
        else:
            message.append("No servers here yet\n\n", style="bold")
            message.append("Pick another tag on the left, or press ")
            message.append_text(keycap("a", accent, ink))
            message.append(" to add a server.")
        empty.update(message)

    def _update_details(self) -> None:
        try:
            box_ = self.query_one("#details")
            body = self.query_one("#details-body", Static)
        except Exception:
            return
        s = self.selected_server()
        if not s:
            box_.border_title = ""
            box_.border_subtitle = ""
            body.update("")
            return
        p = self.probe_for(s)
        pal = self.pal
        box_.border_title = f"{'★ ' if s.favorite else ''}{escape(s.name)}"
        if p.state == "up":
            status = f"[{pal['success']}]● reachable[/]  {p.latency_ms:.0f} ms" if p.latency_ms is not None else f"[{pal['success']}]● reachable[/]"
        elif p.state == "down":
            status = f"[{pal['error']}]● unreachable[/]  {escape(p.error)}"
        elif p.state == "checking":
            status = f"[{pal['warning']}]{SPINNER[self._spin % len(SPINNER)]} checking[/]"
        elif p.state == "jump":
            status = f"[{pal['secondary']}]◇ reached through {escape(s.jump_host)}[/]"
        else:
            status = "[dim]○ not checked yet[/]"
        box_.border_subtitle = status

        none = lambda text: Text(text, style="dim")  # noqa: E731
        agent = Text("on", style=pal["warning"]) if s.forward_agent else none("off")
        banner = p.banner.split("-", 2)[-1] if p.banner else ""
        if s.last_connected:
            last = Text(datetime.fromtimestamp(s.last_connected).strftime("%a %d %b %Y, %H:%M"))
            if time.time() - s.last_connected < 7 * 86400:
                last.append(f"  {ago(s.last_connected)}", style="dim")
        else:
            last = none("never")

        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style="dim", no_wrap=True, width=15)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(style="dim", no_wrap=True, width=17)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_row("Host", Text(s.host), "User", Text(s.user) if s.user else none("ssh default"))
        grid.add_row("Port", Text(str(s.port)), "Agent forwarding", agent)
        grid.add_row("Identity file", Text(s.identity_file) if s.identity_file else none("ssh default"),
                     "Jump host", Text(s.jump_host) if s.jump_host else none("none"))
        grid.add_row("Server software", Text(banner) if banner else none("unknown"),
                     "Sessions", Text(str(s.connect_count)))
        added = Text(datetime.fromtimestamp(s.created_at).strftime("%d %b %Y")) if s.created_at else none("by hand")
        grid.add_row("Last connected", last, "Added", added)

        command = Text("$ ", style="dim")
        command.append(s.display_command(), style=pal["primary"])
        command.append("    c copies this", style="dim italic")
        body.update(Group(grid, Text(""), command))

    def _update_statusbar(self) -> None:
        try:
            left_w = self.query_one("#status-left", Static)
            right_w = self.query_one("#status-right", Static)
        except Exception:
            return
        visible = [self.store.get(i) for i in self.row_ids]
        states = [self.probe_for(s).state for s in visible if s]
        total = len(self.store.servers)
        left = Text()
        if len(self.row_ids) != total:
            left.append(f"Showing {len(self.row_ids)} of {total} servers", style="bold")
        else:
            left.append(f"{total} server{'s' if total != 1 else ''}", style="bold")
        compact = self.size.width < 110
        gap = "   " if compact else "    "
        left.append(f"{gap}● ", style=self.pal["success"])
        left.append(f"{states.count('up')} {'up' if compact else 'reachable'}")
        left.append(f"{gap}● ", style=self.pal["error"])
        left.append(f"{states.count('down')} {'down' if compact else 'unreachable'}")
        if states.count("checking"):
            left.append(f"{gap}{SPINNER[self._spin % len(SPINNER)]} ", style=self.pal["warning"])
            left.append(f"{states.count('checking')} checking")
        if states.count("jump"):
            left.append(f"{gap}◇ ", style=self.pal["secondary"])
            left.append(f"{states.count('jump')} {'via jump' if compact else 'via jump host'}")
        if self.marked:
            hidden = len(self.marked - set(self.row_ids))
            left.append(f"{gap}✓ {len(self.marked)} selected", style=f"bold {self.pal['accent']}")
            if hidden:
                left.append(f" ({hidden} not shown)", style="dim")
            if not compact:
                left.append(f"   ⏎ opens {'them' if len(self.marked) > 1 else 'it'} in new tabs", style="dim")
        left_w.update(left)

        right = Text()
        if self.store.error:
            right.append("✕ servers file has a mistake, changes aren't saved", style=self.pal["error"])
            right_w.update(right)
            return
        if self.size.width >= 110:
            right.append(f"Sorted by {dict(SORTS)[self.sort_mode].lower()}    ")
        if any(p.state == "checking" for p in self.probes.values()):
            right.append("checking now")
        elif self.last_check:
            right.append(f"checked {ago(self.last_check)}")
        right_w.update(right)

    # ---- timers

    def _spin_tick(self) -> None:
        checking = [sid for sid in self.row_ids if self.probes.get(sid, Probe()).state == "checking"]
        if not checking:
            return
        self._spin += 1
        for sid in checking:
            self._refresh_row(sid, ("status",))
        self._update_statusbar()

    def _clock_tick(self) -> None:
        self._ticks += 1
        with suppress(Exception):
            self.query_one("#clock", Static).update(datetime.now().strftime("%a %d %b  %H:%M"))
        if self._ticks % 5 == 0:
            self._update_statusbar()
        if self._ticks % 30 == 0:
            for sid in self.row_ids:
                self._refresh_row(sid, ("last",))
        if self._ticks % 2 == 0:
            self._check_file()

    def _check_file(self) -> None:
        """Pick up edits made to the servers file outside the app."""
        if not self.store.changed_on_disk():
            return
        before = {s.id: (s.host, s.port, s.jump_host) for s in self.store.servers}
        if self.store.reload():
            self.marked &= {s.id for s in self.store.servers}
            self._error_shown = ""
            self.rebuild()
            changed = [s for s in self.store.servers if before.get(s.id) != (s.host, s.port, s.jump_host)]
            self.notify(f"Loaded your changes to {self.store.path.name}.", timeout=4)
            if changed:
                self.run_worker(self._check(changed), group="check-file")
            if self.store.warning:
                self.notify(self.store.warning, severity="warning", timeout=12)
        self._report_file_error()
        self._update_statusbar()

    def _report_file_error(self) -> None:
        if self.store.error and self.store.error != self._error_shown:
            self._error_shown = self.store.error
            self.notify(f"{self.store.error} Fix it and save; SSHStation won't write to the file until then.",
                        title="Couldn't read your servers file", severity="error", timeout=15)

    def _save(self) -> bool:
        if self.store.save():
            return True
        self.notify(f"{self.store.error} Changes made here aren't saved until it's fixed.",
                    title="Servers file needs fixing", severity="error", timeout=10)
        return False

    # ---- reachability

    def check_all(self) -> None:
        self.run_worker(self._check(list(self.store.servers)), group="check-all", exclusive=True)

    def check_one(self, server: Server) -> None:
        self.run_worker(self._check([server]), group=f"check-{server.id}", exclusive=True)

    async def _check(self, servers: List[Server]) -> None:
        targets = [s for s in servers if not s.jump_host]
        for s in servers:
            self.probes[s.id] = Probe("jump") if s.jump_host else Probe("checking")
            self._refresh_row(s.id)
        self._update_details()
        self._update_statusbar()
        timeout = float(self.store.settings.get("probe_timeout") or 3.0)
        gate = asyncio.Semaphore(32)

        async def one(server: Server) -> None:
            async with gate:
                result = await probe(server.host, server.port, timeout)
            self.probes[server.id] = result
            self._refresh_row(server.id)
            current = self.selected_server()
            if current and current.id == server.id:
                self._update_details()

        await asyncio.gather(*(one(s) for s in targets))
        if len(servers) > 1 or not self.last_check:
            self.last_check = time.time()
        if self.sort_mode == "status":
            self._rebuild_table()
        self._update_statusbar()

    # ---- events

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self._update_details()
        self.refresh_bindings()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_open()

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        mode = {"name": "name", "last": "recent", "ping": "status", "status": "status"}.get(str(event.column_key.value))
        if mode and mode != self.sort_mode:
            self.sort_mode = mode
            self.store.settings["sort"] = mode
            self._save()
            self._rebuild_table()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search = event.value
        self._rebuild_table()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.action_focus_table()

    @on(OptionList.OptionHighlighted, "#tags")
    def _tag_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id or "all"
        new = None if option_id == "all" else FAVORITES if option_id == "fav" else option_id[4:]
        if new != self.tag_filter:
            self.tag_filter = new
            self._rebuild_table()

    @on(OptionList.OptionSelected, "#tags")
    def _tag_selected(self) -> None:
        self.action_focus_table()

    def check_action(self, action: str, parameters) -> Optional[bool]:
        if action not in OUR_ACTIONS:
            return True
        if isinstance(self.screen, ModalScreen):
            return False
        tabs = self.backend is not None and bool(self.store.settings.get("enter_opens_tab", True))
        if action == "connect_here" and not tabs:
            return False  # Enter already connects in this window
        if action in ("toggle_mark", "mark_all") and self.backend is None:
            return False  # several servers at once need tabs
        if action == "open" and self.marked:
            return True
        if action in NEEDS_SERVER:
            return True if self.selected_server() else None
        return True

    # ---- actions

    def action_focus_table(self) -> None:
        self.query_one(ServerTable).focus()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        elif self.marked:
            self.marked.clear()
            self._refresh_marks()
        self.action_focus_table()

    # selection

    def action_toggle_mark(self) -> None:
        server = self.selected_server()
        if not server or self.backend is None:
            return
        self.marked.symmetric_difference_update({server.id})
        self._refresh_row(server.id, ("mark", "name"))
        self.query_one(ServerTable).action_cursor_down()
        self._update_statusbar()
        self.refresh_bindings()

    def action_mark_all(self) -> None:
        if self.backend is None:
            return
        shown = set(self.row_ids)
        self.marked = (self.marked - shown) if shown <= self.marked else (self.marked | shown)
        self._refresh_marks()

    def _refresh_marks(self) -> None:
        for sid in self.row_ids:
            self._refresh_row(sid, ("mark", "name"))
        self._update_statusbar()
        self.refresh_bindings()

    # connecting

    def action_open(self) -> None:
        """Enter: open the selected servers (or the highlighted one) in new tabs."""
        tabs = self.backend is not None and bool(self.store.settings.get("enter_opens_tab", True))
        if self.marked and self.backend is not None:
            servers = [s for s in self.visible_servers() if s.id in self.marked]
            servers += [s for s in self.store.servers if s.id in self.marked and s not in servers]
            if len(servers) > 8:
                self.push_screen(
                    ConfirmScreen(f"Open {len(servers)} tabs?",
                                  f"Each selected server opens in its own {self._tab_word()}.",
                                  f"Open {len(servers)} tabs", variant="primary"),
                    lambda ok: self._open_tabs(servers) if ok else None,
                )
            else:
                self._open_tabs(servers)
            return
        server = self.selected_server()
        if not server:
            return
        if tabs:
            self._open_tabs([server])
        else:
            self.action_connect_here()

    def _tab_word(self) -> str:
        return "tmux window" if self.backend == "tmux" else "iTerm2 tab"

    def _open_tabs(self, servers: List[Server]) -> None:
        if not shutil.which("ssh"):
            self.notify("ssh isn't installed or isn't on your PATH.", severity="error")
            return
        self._open_tabs_worker([s.id for s in servers])

    @work(thread=True, group="tabs", exit_on_error=False)
    def _open_tabs_worker(self, ids: List[str]) -> None:
        servers = [s for s in (self.store.get(i) for i in ids) if s]
        error = open_in_tabs(servers, self.store.settings)
        if self.is_running:
            with suppress(RuntimeError):  # the app may have closed while the tabs were opening
                self.call_from_thread(self._tabs_opened, ids, error)

    def _tabs_opened(self, ids: List[str], error: Optional[str]) -> None:
        if error:
            self.notify(error, title="Couldn't open a new tab", severity="error", timeout=10)
            return
        self._record_connections(ids)
        opened = [s.name for s in (self.store.get(i) for i in ids) if s]
        if len(ids) > 1:
            self.marked.clear()
            self.rebuild()
            self.notify(", ".join(opened), title=f"Opened {len(ids)} servers in new {self._tab_word()}s")
        else:
            self.rebuild()
            self.notify(f"Opened {opened[0] if opened else 'the server'} in a new {self._tab_word()}.")

    def _record_connections(self, ids: List[str]) -> None:
        now = time.time()
        for sid in ids:
            server = self.store.get(sid)
            if server:
                server.last_connected = now
                server.connect_count += 1
        self._save()

    def action_connect_here(self) -> None:
        """Connect in this window; SSHStation comes back when the session ends."""
        server = self.selected_server()
        if not server:
            return
        if not shutil.which("ssh"):
            self.notify("ssh isn't installed or isn't on your PATH.", severity="error")
            return
        try:
            with self.suspend():
                code, elapsed = run_session(server, self.store.settings)
        except SuspendNotSupported:
            self.notify("This terminal can't hand over to ssh. Press c to copy the command instead.",
                        severity="warning")
            return
        self._after_session(server, code, elapsed)

    def _after_session(self, server: Server, code: int, elapsed: float) -> None:
        if code in (255, 127):
            self.notify(f"Couldn't connect to {server.name}. ssh exited with code {code}.",
                        title="Connection failed", severity="error", timeout=8)
        else:
            self._record_connections([server.id])
            self.notify(f"Session lasted {duration(elapsed)}.", title=f"Disconnected from {server.name}")
        self._check_file()
        self.rebuild(keep_id=server.id)
        current = self.store.get(server.id)
        if current:
            self.check_one(current)

    def action_edit_file(self) -> None:
        """Open the servers file in the user's editor; changes load when it's saved."""
        path = self.store.path
        if path is None:
            self.notify("Demo mode has no servers file.", severity="warning")
            return
        if not path.exists() and not self._save():
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor:
            try:
                with self.suspend():
                    subprocess.call(shlex.split(editor) + [str(path)])
            except (SuspendNotSupported, OSError) as exc:
                self.notify(f"Couldn't start {editor}: {exc}", severity="error")
                return
            self._check_file()
        elif shutil.which("open"):
            subprocess.Popen(["open", "-t", str(path)])
            self.notify("Your changes load here as soon as you save the file.",
                        title=f"Opened {path.name} in your text editor", timeout=6)
        else:
            self.notify(f"Set $EDITOR, or open {path} yourself.", severity="warning")

    def action_copy(self) -> None:
        server = self.selected_server()
        if not server:
            return
        command = server.shell_command()
        if not copy_text(command):
            self.copy_to_clipboard(command)
        self.notify(server.display_command(), title="Copied ssh command")

    def action_add(self) -> None:
        self.push_screen(ServerForm(self.store), self._added)

    def action_duplicate(self) -> None:
        server = self.selected_server()
        if not server:
            return
        copy = replace(server, name=self.store.unique_name(f"{server.name}-copy"))
        self.push_screen(ServerForm(self.store, copy, title=f"Duplicate {server.name}"), self._added)

    def _added(self, server: Optional[Server]) -> None:
        if not server:
            return
        self.store.upsert(server)
        self._save()
        self.rebuild(keep_id=server.id)
        self.action_focus_table()
        self.notify(f"{server.target}:{server.port}", title=f"Added {server.name}")
        self.check_one(server)

    def action_edit(self) -> None:
        server = self.selected_server()
        if server:
            self.push_screen(ServerForm(self.store, server, editing=True, title=f"Edit {server.name}"), self._edited)

    def _edited(self, server: Optional[Server]) -> None:
        if not server:
            return
        before = self.store.get(server.id)
        self.store.upsert(server)
        self._save()
        self.rebuild(keep_id=server.id)
        self.notify(f"Saved changes to {server.name}.")
        if not before or (before.host, before.port, before.jump_host) != (server.host, server.port, server.jump_host):
            self.check_one(server)

    def action_delete(self) -> None:
        server = self.selected_server()
        if not server:
            return

        def done(confirmed: Optional[bool]) -> None:
            if confirmed:
                index = self.row_ids.index(server.id) if server.id in self.row_ids else 0
                self.store.remove(server.id)
                self.probes.pop(server.id, None)
                self._save()
                neighbours = [i for i in self.row_ids if i != server.id]
                keep = neighbours[min(index, len(neighbours) - 1)] if neighbours else None
                self.rebuild(keep_id=keep)
                self.notify(f"Deleted {server.name}.")

        self.push_screen(
            ConfirmScreen(f"Delete {server.name}?",
                          f"{server.target}:{server.port} will be removed from {PROG}. "
                          "Your ~/.ssh/config and keys aren't touched.",
                          "Delete server"),
            done,
        )

    def action_favorite(self) -> None:
        server = self.selected_server()
        if not server:
            return
        server.favorite = not server.favorite
        self._save()
        self.rebuild(keep_id=server.id)

    def action_recheck(self) -> None:
        self.check_all()

    def action_cycle_sort(self) -> None:
        keys = [k for k, _ in SORTS]
        self.sort_mode = keys[(keys.index(self.sort_mode) + 1) % len(keys)]
        self.store.settings["sort"] = self.sort_mode
        self._save()
        self._rebuild_table()

    def action_toggle_sidebar(self) -> None:
        show = not bool(self.store.settings.get("show_sidebar", True))
        self.store.settings["show_sidebar"] = show
        self._save()
        self._apply_layout(self.size.width, self.size.height)
        if show and self._narrow:
            self.notify("The tag list returns when the window is at least 100 columns wide.")

    def action_import_config(self) -> None:
        entries = parse_ssh_config()
        if not entries:
            self.notify("No hosts found in ~/.ssh/config.", severity="warning")
            return
        self.push_screen(ImportScreen(entries, self.store), self._imported)

    def _imported(self, entries: Optional[List[dict]]) -> None:
        if not entries:
            return
        added = []
        for entry in entries:
            server = Server.from_dict(dict(entry, name=self.store.unique_name(entry["name"])))
            self.store.upsert(server)
            added.append(server)
        self._save()
        self.rebuild(keep_id=added[0].id)
        self.notify(f"Imported {len(added)} server{'s' if len(added) != 1 else ''} from ~/.ssh/config.")
        self.run_worker(self._check(added), group="check-import")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


# --------------------------------------------------------------------------- CLI


def find_matches(store: Store, query: str) -> List[Server]:
    q = query.lower()
    exact = [s for s in store.servers if s.name.lower() == q]
    if exact:
        return exact
    prefix = [s for s in store.servers if s.name.lower().startswith(q)]
    if prefix:
        return prefix
    return [s for s in store.servers
            if q in " ".join([s.name, s.host, s.user, *s.tags]).lower()]


def print_list(store: Store, check: bool) -> None:
    console = Console()
    if not store.servers:
        console.print(f"No servers yet. Run [bold]{PROG}[/] and press [bold]a[/] to add one.")
        return
    results: Dict[str, Probe] = {}
    if check:
        timeout = float(store.settings.get("probe_timeout") or 3.0)

        async def run() -> None:
            async def one(s: Server) -> None:
                results[s.id] = Probe("jump") if s.jump_host else await probe(s.host, s.port, timeout)
            await asyncio.gather(*(one(s) for s in store.servers))

        with console.status("Checking servers…"):
            asyncio.run(run())

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    if check:
        table.add_column("")
    for column in ("Name", "Host", "Port", "User", "Tags", "Last connected"):
        table.add_column(column, justify="right" if column == "Port" else "left")
    for s in sorted(store.servers, key=lambda s: (not s.favorite, s.name.lower())):
        row = []
        if check:
            state = results[s.id].state
            row.append({"up": "[green]●[/]", "down": "[red]●[/]", "jump": "[blue]◇[/]"}.get(state, "○"))
        row += [("★ " if s.favorite else "") + escape(s.name), escape(s.host), str(s.port),
                escape(s.user) or "[dim]—[/]", escape(", ".join(s.tags)), ago(s.last_connected)]
        table.add_row(*row)
    console.print(table)


def edit_file(path: Path) -> int:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return subprocess.call(shlex.split(editor) + [str(path)])
    if shutil.which("open"):
        return subprocess.call(["open", "-t", str(path)])
    print(path)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=f"{APP_NAME}: a refined SSH server manager for the terminal. An Idea by {AUTHOR}.",
    )
    parser.add_argument("query", nargs="?", help="connect straight to a server by name; opens the list if several match")
    parser.add_argument("-l", "--list", action="store_true", help="print your servers and exit")
    parser.add_argument("--check", action="store_true", help="with --list, also check which servers are reachable")
    parser.add_argument("--edit", action="store_true", help="open the servers file in your editor")
    parser.add_argument("--config", action="store_true", help="print where your servers are stored")
    parser.add_argument("--demo", action="store_true", help="open with sample servers; nothing is saved")
    parser.add_argument("--open", metavar="ID", help=argparse.SUPPRESS)  # used by new tabs
    parser.add_argument("-V", "--version", action="version", version=f"{APP_NAME} {__version__}, an idea by {AUTHOR}")
    args = parser.parse_args(argv)

    path = default_store_path()
    if args.config:
        print(path)
        return 0

    migrated, migrated_from = 0, ""
    if args.demo:
        store = demo_store()
    else:
        migrated, migrated_from = migrate_previous(path)
        store = Store(path).load()
        if not path.exists():
            store.save()  # create it so it can be edited by hand straight away

    if args.edit:
        return edit_file(path)

    if args.list or args.query or args.open:
        if store.error:
            print(store.error, file=sys.stderr)
        elif store.warning:
            print(store.warning, file=sys.stderr)

    if args.open:
        server = store.find(args.open)
        if not server:
            print(f"\n  No server with the id “{args.open}” is in {path}.")
            with suppress(EOFError, KeyboardInterrupt):
                input("  Press Enter to close. ")
            return 1
        code, _ = run_session(server, store.settings, pause_on_error=True)
        return code

    if args.list:
        print_list(store, args.check)
        return 1 if store.error else 0

    initial = ""
    if args.query and not args.demo:
        matches = find_matches(store, args.query)
        if not matches:
            print(f"No server matches “{args.query}”. Run `{PROG}` to see the list.", file=sys.stderr)
            return 1
        if len(matches) == 1:
            server = matches[0]
            code, _ = run_session(server, store.settings, pause_on_error=False)
            if code not in (255, 127):
                server.last_connected = time.time()
                server.connect_count += 1
                store.save()
            return code
        initial = args.query

    SSHStationApp(store, initial_search=initial, demo=args.demo, migrated=(migrated, migrated_from)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
