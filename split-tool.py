#!/usr/bin/env python3
"""
ModSplit GUI - определяет client/server side модов с графическим интерфейсом.

Запуск: python mod_sorter_gui.py
При старте ищет папку "mods" рядом со скриптом. Если не находит -
открывает диалог выбора папки.
"""

import sys
import os
import subprocess


def ensure_requests():
    try:
        import requests  # noqa
        return
    except ImportError:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "requests"])
        except Exception:
            pass


ensure_requests()

import json
import hashlib
import zipfile
import struct
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
CURSEFORGE_API_KEY = ""  # вставь свой ключ с console.curseforge.com (опционально)

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
TIMEOUT = 15
HEADERS_MODRINTH = {"User-Agent": "modsplit-gui/2.0 (personal use)"}

# key -> (заголовок, emoji-иконка, цвет фона заголовка, пояснение)
GROUP_META = {
    "required_both": (
        "Обязательны везде", "🎮🖥️", "#3f6b4f",
        "Нужны и на клиенте, и на сервере — ставь в обе сборки.",
    ),
    "client_only": (
        "Только клиент", "🎮", "#2f5d8a",
        "На сервере не используются — можно смело убрать из server-pack.",
    ),
    "server_only": (
        "Только сервер", "🖥️", "#7a52a8",
        "Клиенту не нужны — не клади в client-pack.",
    ),
    "server_required_client_optional": (
        "Сервер: обязателен / Клиент: опционален", "🖥️➕🎮", "#b4762a",
        "На сервере должен стоять, клиенту можно не ставить (например серверная логика без визуала).",
    ),
    "optional_both": (
        "Опционален везде", "➕", "#4c7280",
        "Не обязателен ни клиенту, ни серверу — на усмотрение.",
    ),
    "unclear": (
        "Неясно", "❓", "#777777",
        "Нет данных ни на Modrinth/CurseForge, ни в самом jar — проверь вручную.",
    ),
}
GROUP_ORDER = ["required_both", "client_only", "server_only",
               "server_required_client_optional", "optional_both", "unclear"]


# ---------------------------------------------------------------------------
# Хеши
# ---------------------------------------------------------------------------
def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def curseforge_murmur2_hash(path: Path) -> int:
    with open(path, "rb") as f:
        data = f.read()
    filtered = bytes(b for b in data if b not in (9, 10, 13, 32))

    def murmur2_32(buf: bytes, seed: int = 1) -> int:
        m = 0x5BD1E995
        r = 24
        length = len(buf)
        h = (seed ^ length) & 0xFFFFFFFF
        i = 0
        while length >= 4:
            k = struct.unpack_from("<I", buf, i)[0]
            k = (k * m) & 0xFFFFFFFF
            k ^= k >> r
            k = (k * m) & 0xFFFFFFFF
            h = (h * m) & 0xFFFFFFFF
            h ^= k
            i += 4
            length -= 4
        rem = len(buf) - i
        if rem == 3:
            h ^= buf[i + 2] << 16
        if rem >= 2:
            h ^= buf[i + 1] << 8
        if rem >= 1:
            h ^= buf[i]
            h = (h * m) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * m) & 0xFFFFFFFF
        h ^= h >> 15
        return h & 0xFFFFFFFF

    return murmur2_32(filtered)


# ---------------------------------------------------------------------------
# Modrinth / CurseForge
# ---------------------------------------------------------------------------
def query_modrinth_batch(hashes, log):
    if not hashes:
        return {}
    try:
        r = requests.post(
            f"{MODRINTH_API}/version_files",
            headers=HEADERS_MODRINTH,
            json={"hashes": hashes, "algorithm": "sha1"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"Modrinth ответил кодом {r.status_code}, пропускаю.")
            return {}
        return r.json()
    except Exception as e:
        log(f"Нет связи с Modrinth ({e}).")
        return {}


def get_modrinth_project_sides(project_id, log):
    try:
        r = requests.get(f"{MODRINTH_API}/project/{project_id}", headers=HEADERS_MODRINTH, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("client_side", "unknown"), data.get("server_side", "unknown")
    except Exception:
        return None


def query_curseforge_by_fingerprint(fingerprints, log):
    if not CURSEFORGE_API_KEY or not fingerprints:
        return {}
    try:
        r = requests.post(
            f"{CURSEFORGE_API}/fingerprints",
            headers={"x-api-key": CURSEFORGE_API_KEY, "Accept": "application/json"},
            json={"fingerprints": fingerprints},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"CurseForge ответил кодом {r.status_code}, пропускаю.")
            return {}
        data = r.json()
        matches = data.get("data", {}).get("exactMatches", [])
        result = {}
        for m in matches:
            fp = m.get("file", {}).get("fileFingerprint")
            if fp is not None:
                result[fp] = {"mod_id": m.get("id")}
        return result
    except Exception as e:
        log(f"Нет связи с CurseForge ({e}).")
        return {}


# ---------------------------------------------------------------------------
# Локальные метаданные jar
# ---------------------------------------------------------------------------
def read_fabric_environment(jar_path: Path):
    try:
        with zipfile.ZipFile(jar_path) as z:
            if "fabric.mod.json" not in z.namelist():
                return None
            raw = z.read("fabric.mod.json").decode("utf-8", errors="ignore")
            return json.loads(raw).get("environment")
    except Exception:
        return None


def read_forge_toml(jar_path: Path):
    try:
        with zipfile.ZipFile(jar_path) as z:
            candidates = [n for n in z.namelist() if n.endswith("mods.toml")]
            if not candidates:
                return None
            text = z.read(candidates[0]).decode("utf-8", errors="ignore")
            if "IGNORE_SERVER_ONLY" in text or 'side="SERVER"' in text or 'side = "SERVER"' in text:
                return "server"
            if 'side="CLIENT"' in text or 'side = "CLIENT"' in text:
                return "client"
            return "unknown"
    except Exception:
        return None


def classify_local(jar_path: Path):
    env = read_fabric_environment(jar_path)
    if env == "client":
        return "client_only", "client-only (Fabric metadata)"
    if env == "server":
        return "server_only", "server-only (Fabric metadata)"
    if env == "*":
        return "unclear", "both, точный статус неизвестен (Fabric metadata)"

    forge = read_forge_toml(jar_path)
    if forge == "client":
        return "client_only", "client-only (Forge, эвристика)"
    if forge == "server":
        return "server_only", "server-only (Forge, эвристика)"

    return "unclear", "нет данных ни в jar, ни на Modrinth/CurseForge"


def classify_modrinth(cs: str, ss: str):
    if cs == "required" and ss == "required":
        return "required_both", f"client={cs}, server={ss}"
    if cs == "required" and ss == "unsupported":
        return "client_only", f"client={cs}, server={ss}"
    if cs == "unsupported" and ss == "required":
        return "server_only", f"client={cs}, server={ss}"
    if cs == "optional" and ss == "required":
        return "server_required_client_optional", f"client={cs}, server={ss}"
    if cs == "optional" and ss == "optional":
        return "optional_both", f"client={cs}, server={ss}"
    return "unclear", f"client={cs}, server={ss}"


# ---------------------------------------------------------------------------
# Сканирование
# ---------------------------------------------------------------------------
def scan_mods(mods_dir: Path, log, progress):
    jars = sorted(mods_dir.glob("*.jar"))
    if not jars:
        return [], []

    broken = []
    hash_to_path = {}
    for jar in jars:
        try:
            hash_to_path[sha1_of_file(jar)] = jar
        except Exception as e:
            broken.append((jar.name, str(e)))

    log(f"Найдено модов: {len(jars)}. Запрашиваю Modrinth...")
    modrinth_data = query_modrinth_batch(list(hash_to_path.keys()), log) if requests else {}

    results = []
    leftover = []

    total = len(hash_to_path)
    done = 0
    for h, jar in hash_to_path.items():
        done += 1
        progress(done, total)
        info = modrinth_data.get(h)
        if info:
            project_id = info.get("project_id")
            sides = get_modrinth_project_sides(project_id, log) if project_id else None
            if sides:
                group, details = classify_modrinth(sides[0], sides[1])
                results.append((jar.name, group, details, "Modrinth"))
                continue
        leftover.append(jar)

    if leftover and CURSEFORGE_API_KEY and requests:
        log(f"Не найдено на Modrinth: {len(leftover)}. Пробую CurseForge...")
        fp_map = {}
        for jar in leftover:
            try:
                fp_map[curseforge_murmur2_hash(jar)] = jar
            except Exception:
                continue
        cf_data = query_curseforge_by_fingerprint(list(fp_map.keys()), log)
        still = []
        for fp, jar in fp_map.items():
            if cf_data.get(fp):
                results.append((jar.name, "unclear", "найден на CurseForge (точная сторона недоступна)", "CurseForge"))
            else:
                still.append(jar)
        leftover = still
    elif leftover and not CURSEFORGE_API_KEY:
        log(f"Не найдено на Modrinth: {len(leftover)}. CurseForge пропущен (нет ключа).")

    for jar in leftover:
        group, details = classify_local(jar)
        results.append((jar.name, group, details, "локально"))

    results.sort(key=lambda x: x[0].lower())
    return results, broken


# ---------------------------------------------------------------------------
# Тёмная минималистичная тема
# ---------------------------------------------------------------------------
BG = "#121212"
BG_PANEL = "#1a1a1a"
BG_FIELD = "#1f1f1f"
FG = "#e6e6e6"
FG_DIM = "#9a9a9a"
ACCENT = "#3a3a3a"
BORDER = "#2a2a2a"


def apply_dark_theme(root: tk.Tk):
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG, foreground=FG, fieldbackground=BG_FIELD,
                     bordercolor=BORDER, lightcolor=BG, darkcolor=BG, font=("Segoe UI", 9))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TButton", background=ACCENT, foreground=FG, borderwidth=0,
                     focusthickness=0, padding=6)
    style.map("TButton", background=[("active", "#4a4a4a"), ("disabled", "#262626")],
              foreground=[("disabled", "#666666")])
    style.configure("TScrollbar", background=ACCENT, troughcolor=BG, bordercolor=BG,
                     arrowcolor=FG)
    style.configure("Horizontal.TProgressbar", background="#5a5a5a", troughcolor=BG_PANEL,
                     bordercolor=BG, lightcolor="#5a5a5a", darkcolor="#5a5a5a")
    style.configure("Treeview", background=BG_PANEL, fieldbackground=BG_PANEL, foreground=FG,
                     bordercolor=BORDER, rowheight=24)
    style.configure("Treeview.Heading", background=ACCENT, foreground=FG, relief="flat")
    style.map("Treeview.Heading", background=[("active", "#4a4a4a")])
    style.map("Treeview", background=[("selected", "#333333")], foreground=[("selected", FG)])


def tint_for_dark(hex_color: str) -> str:
    """Затемняет цвет категории, чтобы текст оставался читаемым на тёмном фоне."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    bg_r, bg_g, bg_b = (0x1a, 0x1a, 0x1a)
    mix = 0.35
    r = int(r * mix + bg_r * (1 - mix))
    g = int(g * mix + bg_g * (1 - mix))
    b = int(b * mix + bg_b * (1 - mix))
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Копирование файлов
# ---------------------------------------------------------------------------
import shutil
from datetime import datetime


def copy_selection(client_items, server_items, mods_dir: Path, log):
    out_dir = mods_dir.parent / f"ModSplit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    client_dir = out_dir / "client"
    server_dir = out_dir / "server"
    client_dir.mkdir(parents=True, exist_ok=True)
    server_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = []

    for name in client_items:
        src_path = mods_dir / name
        try:
            shutil.copy2(src_path, client_dir / name)
            copied += 1
        except Exception as e:
            skipped.append((name, f"client: {e}"))

    for name in server_items:
        src_path = mods_dir / name
        try:
            shutil.copy2(src_path, server_dir / name)
            copied += 1
        except Exception as e:
            skipped.append((name, f"server: {e}"))

    log(f"Готово: скопировано {copied} файлов.")
    return out_dir, copied, skipped


# ---------------------------------------------------------------------------
# Чек-лист со скроллом
# ---------------------------------------------------------------------------
class CheckListPanel(ttk.Frame):
    def __init__(self, parent, title):
        super().__init__(parent, padding=6)
        ttk.Label(self, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0, bg=BG_PANEL)
        vsb = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG_PANEL)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win, width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _wheel(event):
            delta = -1 if event.num == 5 or event.delta < 0 else 1
            self.canvas.yview_scroll(-delta, "units")

        self.canvas.bind("<Enter>", lambda e: (
            self.canvas.bind_all("<MouseWheel>", _wheel),
            self.canvas.bind_all("<Button-4>", _wheel),
            self.canvas.bind_all("<Button-5>", _wheel),
        ))
        self.canvas.bind("<Leave>", lambda e: (
            self.canvas.unbind_all("<MouseWheel>"),
            self.canvas.unbind_all("<Button-4>"),
            self.canvas.unbind_all("<Button-5>"),
        ))

        self.vars = {}      # name -> BooleanVar
        self.rows = {}      # name -> Checkbutton widget
        self.groups = {}    # name -> group_key (для группового удаления опциональных)

    def add_item(self, name, checked=True, note="", group=None):
        if name in self.vars:
            return
        var = tk.BooleanVar(value=checked)
        text = f"{name}   {note}".rstrip()
        cb = tk.Checkbutton(
            self.inner, text=text, variable=var,
            bg=BG_PANEL, fg=FG, selectcolor=BG_FIELD, activebackground=BG_PANEL,
            activeforeground=FG, anchor="w", font=("Segoe UI", 9), highlightthickness=0,
            bd=0,
        )
        cb.pack(anchor="w", fill="x", pady=1, padx=2)
        self.vars[name] = var
        self.rows[name] = cb
        self.groups[name] = group

    def remove_by_group(self, group_keys):
        to_remove = [name for name, g in self.groups.items() if g in group_keys]
        for name in to_remove:
            self.rows[name].destroy()
            del self.rows[name]
            del self.vars[name]
            del self.groups[name]

    def has_group(self, group_key) -> bool:
        return group_key in self.groups.values()

    def get_checked(self):
        return [name for name, var in self.vars.items() if var.get()]


class DistributeDialog(tk.Toplevel):
    def __init__(self, parent, results, mods_dir: Path):
        super().__init__(parent)
        self.configure(bg=BG)
        self.title("Разложить по папкам")
        self.geometry("820x600")
        self.minsize(640, 460)
        self.transient(parent)
        self.grab_set()

        self.results = results
        self.mods_dir = mods_dir
        self.client_optional_on = False
        self.server_optional_on = False

        self.by_group = {key: [] for key in GROUP_ORDER}
        for name, group, details, source in results:
            self.by_group[group].append(name)

        info = ttk.Label(
            self,
            text="Клиент: обязательные везде + только-клиентские.  "
                 "Сервер: обязательные везде + только-серверные + обязательные на сервере.\n"
                 "Опциональные моды переключаются кнопками ниже (повторное нажатие убирает их обратно).",
            wraplength=780, justify="left", foreground=FG_DIM,
        )
        info.pack(fill="x", padx=10, pady=(10, 6))

        panels = ttk.Frame(self)
        panels.pack(fill="both", expand=True, padx=10)
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        panels.rowconfigure(0, weight=1)

        self.client_panel = CheckListPanel(panels, "🎮 Клиент")
        self.client_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.server_panel = CheckListPanel(panels, "🖥️ Сервер")
        self.server_panel.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        for name in self.by_group["required_both"]:
            self.client_panel.add_item(name, checked=True, note="(обязателен везде)", group="required_both")
            self.server_panel.add_item(name, checked=True, note="(обязателен везде)", group="required_both")
        for name in self.by_group["client_only"]:
            self.client_panel.add_item(name, checked=True, note="(только клиент)", group="client_only")
        for name in self.by_group["server_only"]:
            self.server_panel.add_item(name, checked=True, note="(только сервер)", group="server_only")
        for name in self.by_group["server_required_client_optional"]:
            self.server_panel.add_item(name, checked=True, note="(обязателен на сервере)",
                                        group="server_required_client_optional")

        add_row = ttk.Frame(self)
        add_row.pack(fill="x", padx=10, pady=8)
        self.client_opt_btn = ttk.Button(add_row, text="+ Опциональные на клиент",
                                          command=self._toggle_optional_client)
        self.client_opt_btn.pack(side="left", padx=(0, 6))
        self.server_opt_btn = ttk.Button(add_row, text="+ Опциональные на сервер",
                                          command=self._toggle_optional_server)
        self.server_opt_btn.pack(side="left")

        if self.by_group["unclear"]:
            ttk.Label(
                add_row,
                text=f"⚠ {len(self.by_group['unclear'])} мод(ов) с неясной категорией не добавлены автоматически.",
                foreground="#c9933f",
            ).pack(side="right")

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Отмена", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="📦 Распределить", command=self._do_distribute).pack(side="right")

    def _toggle_optional_client(self):
        if self.client_optional_on:
            self.client_panel.remove_by_group({"server_required_client_optional", "optional_both"})
            self.client_opt_btn.config(text="+ Опциональные на клиент")
            self.client_optional_on = False
        else:
            for name in self.by_group["server_required_client_optional"]:
                self.client_panel.add_item(name, checked=True, note="(опционален на клиенте)",
                                            group="server_required_client_optional")
            for name in self.by_group["optional_both"]:
                self.client_panel.add_item(name, checked=True, note="(опционален везде)",
                                            group="optional_both")
            self.client_opt_btn.config(text="− Убрать опциональные с клиента")
            self.client_optional_on = True

    def _toggle_optional_server(self):
        if self.server_optional_on:
            self.server_panel.remove_by_group({"optional_both"})
            self.server_opt_btn.config(text="+ Опциональные на сервер")
            self.server_optional_on = False
        else:
            for name in self.by_group["optional_both"]:
                self.server_panel.add_item(name, checked=True, note="(опционален везде)",
                                            group="optional_both")
            self.server_opt_btn.config(text="− Убрать опциональные с сервера")
            self.server_optional_on = True

    def _do_distribute(self):
        client_items = self.client_panel.get_checked()
        server_items = self.server_panel.get_checked()
        if not client_items and not server_items:
            messagebox.showinfo("ModSplit", "Ничего не выбрано.")
            return
        try:
            out_dir, copied, skipped = copy_selection(client_items, server_items, self.mods_dir, lambda m: None)
        except Exception as e:
            messagebox.showerror("ModSplit", f"Ошибка копирования: {e}")
            return

        text = f"Скопировано: {copied}\nПапка: {out_dir}"
        if skipped:
            text += f"\n\nОшибки ({len(skipped)}):\n" + "\n".join(f"- {n}: {r}" for n, r in skipped[:15])
        messagebox.showinfo("ModSplit — готово", text)
        self.destroy()


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_dark_theme(self)

        self.title("ModSplit")
        self.geometry("820x640")
        self.minsize(580, 440)

        self.mods_dir = self._guess_mods_dir()

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        self.path_var = tk.StringVar(value=str(self.mods_dir) if self.mods_dir else "Папка не выбрана")
        ttk.Label(top, textvariable=self.path_var, foreground=FG_DIM).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Папка...", command=self.choose_folder).pack(side="left", padx=4)
        self.scan_btn = ttk.Button(top, text="Сканировать", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=4)

        legend = ttk.Frame(self, padding=(12, 0))
        legend.pack(fill="x")
        ttk.Label(legend, text="🎮 клиент   🖥️ сервер   🎮🖥️ оба   ➕ опционально   ❓ неясно",
                  foreground=FG_DIM).pack(anchor="w")

        self.status_var = tk.StringVar(value="Готов.")
        ttk.Label(self, textvariable=self.status_var, padding=(12, 6)).pack(fill="x")
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(0, 8))

        table_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        table_frame.pack(fill="both", expand=True)

        columns = ("icon", "name", "category", "source")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="none")
        self.tree.heading("icon", text="")
        self.tree.heading("name", text="Мод")
        self.tree.heading("category", text="Категория")
        self.tree.heading("source", text="Источник")
        self.tree.column("icon", width=70, anchor="center", stretch=False)
        self.tree.column("name", width=340, anchor="w")
        self.tree.column("category", width=260, anchor="w")
        self.tree.column("source", width=80, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        for key in GROUP_ORDER:
            _, _, color, _ = GROUP_META[key]
            self.tree.tag_configure(key, background=tint_for_dark(color), foreground=FG)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        self.distribute_btn = ttk.Button(bottom, text="📦 Разложить по папкам", command=self.open_distribute_dialog)
        self.distribute_btn.pack(side="right")

        self.results = []
        self.broken = []

        if not self.mods_dir:
            self.after(300, self.choose_folder)

    def _guess_mods_dir(self):
        candidate = Path(__file__).resolve().parent / "mods"
        if candidate.is_dir():
            return candidate
        return None

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Выбери папку с модами (mods)")
        if folder:
            self.mods_dir = Path(folder)
            self.path_var.set(str(self.mods_dir))

    def log(self, msg):
        self.status_var.set(msg)
        self.update_idletasks()

    def set_progress(self, done, total):
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = done
        self.update_idletasks()

    def start_scan(self):
        if not self.mods_dir or not self.mods_dir.is_dir():
            messagebox.showwarning("ModSplit", "Сначала выбери папку mods.")
            return
        if requests is None:
            messagebox.showerror("ModSplit", "Установи модуль: pip install requests")
            return

        self.scan_btn.config(state="disabled")
        self.distribute_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())

        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            results, broken = scan_mods(self.mods_dir, self.log, self.set_progress)
        except Exception as e:
            self.log(f"Ошибка: {e}")
            self.scan_btn.config(state="normal")
            return
        self.results = results
        self.broken = broken
        self.after(0, self._populate_results)

    def _populate_results(self):
        order_index = {key: i for i, key in enumerate(GROUP_ORDER)}
        sorted_results = sorted(self.results, key=lambda r: (order_index.get(r[1], 99), r[0].lower()))

        for name, group, details, source in sorted_results:
            title, emoji, color, hint = GROUP_META[group]
            self.tree.insert("", "end", values=(emoji, name, title, source), tags=(group,))

        msg = f"Готово. Модов: {len(self.results)}."
        if self.broken:
            msg += f" Не прочитано: {len(self.broken)}."
        self.log(msg)
        self.scan_btn.config(state="normal")
        self.distribute_btn.config(state="normal" if self.results else "disabled")

    def open_distribute_dialog(self):
        if not self.results:
            messagebox.showinfo("ModSplit", "Сначала запусти сканирование.")
            return
        DistributeDialog(self, self.results, self.mods_dir)


if __name__ == "__main__":
    app = App()
    app.mainloop()
