#!/usr/bin/env python3
"""
Mod Sorter - определяет, какие jar-моды нужны на клиенте, а какие только
на сервере (и можно ли их не ставить клиенту).

Просто запусти этот файл (двойной клик или "python mod_sorter.py"),
выбери папку с модами в открывшемся окне - и получи отчёт.

Источники данных, по порядку (если предыдущий не дал ответа):
  1. Modrinth API (по SHA1-хешу файла)
  2. CurseForge API (по murmur2-хешу файла) - нужен бесплатный API-ключ,
     см. CURSEFORGE_API_KEY ниже. Без ключа этот шаг просто пропускается.
  3. Локальные метаданные внутри jar: fabric.mod.json (Fabric) или
     mods.toml (Forge/NeoForge)
  4. Если вообще ничего не найдено - мод помечается "неизвестно",
     программа не падает.

Ничего, кроме requests, не требуется - при отсутствии он попытается
установиться автоматически.
"""

import sys
import os
import subprocess

# ---------------------------------------------------------------------------
# 0. Автоустановка requests, если его нет
# ---------------------------------------------------------------------------
def ensure_requests():
    try:
        import requests  # noqa
        return
    except ImportError:
        print("Модуль 'requests' не найден, пробую установить...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "requests"])
            print("Установлено успешно.")
        except Exception as e:
            print(f"Не удалось установить requests автоматически: {e}")
            print("Установи вручную: pip install requests")
            input("Нажми Enter для выхода...")
            sys.exit(1)


ensure_requests()

import json
import hashlib
import zipfile
import struct
from pathlib import Path
import requests

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Бесплатный ключ можно получить на https://console.curseforge.com/
# Если оставить пустым - шаг CurseForge просто пропускается, ошибки не будет.
CURSEFORGE_API_KEY = ""

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
TIMEOUT = 15

HEADERS_MODRINTH = {"User-Agent": "mod-sorter-script/2.0 (personal use)"}


# ---------------------------------------------------------------------------
# 1. Выбор папки - графически, с fallback на ручной ввод пути
# ---------------------------------------------------------------------------
def pick_mods_folder() -> Path:
    # Если путь передан аргументом командной строки - используем его
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.is_dir():
            return p
        print(f"Указанный путь не найден: {p}\n")

    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        print("Открываю окно выбора папки...")
        folder = filedialog.askdirectory(title="Выбери папку с модами (mods)")
        root.destroy()
        if folder:
            return Path(folder)
        print("Папка не выбрана.")
    except Exception as e:
        print(f"Графический выбор папки недоступен ({e}), вводи путь вручную.")

    while True:
        path_str = input("Введи полный путь к папке mods (или 'q' для выхода): ").strip().strip('"')
        if path_str.lower() == "q":
            sys.exit(0)
        p = Path(path_str)
        if p.is_dir():
            return p
        print(f"Папка не найдена: {p}\n")


# ---------------------------------------------------------------------------
# 2. Хеши
# ---------------------------------------------------------------------------
def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def curseforge_murmur2_hash(path: Path) -> int:
    """CurseForge использует murmur2 (32-bit, seed=1) от содержимого файла
    с вырезанными байтами 9, 10, 13, 32 (пробельные символы)."""
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
# 3. Modrinth
# ---------------------------------------------------------------------------
def query_modrinth_batch(hashes: list) -> dict:
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
            print(f"  [!] Modrinth ответил кодом {r.status_code}, пропускаю этот источник.")
            return {}
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"  [!] Нет связи с Modrinth ({e}). Пропускаю этот источник.")
        return {}
    except Exception as e:
        print(f"  [!] Неожиданная ошибка Modrinth: {e}")
        return {}


def get_modrinth_project_sides(project_id: str):
    try:
        r = requests.get(f"{MODRINTH_API}/project/{project_id}", headers=HEADERS_MODRINTH, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("client_side", "unknown"), data.get("server_side", "unknown")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. CurseForge (опционально, требует ключ)
# ---------------------------------------------------------------------------
def query_curseforge_by_fingerprint(fingerprints: list) -> dict:
    """Возвращает {fingerprint: {client: bool-ish, server: bool-ish, name: str}}"""
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
            print(f"  [!] CurseForge ответил кодом {r.status_code}, пропускаю этот источник.")
            return {}
        data = r.json()
        matches = data.get("data", {}).get("exactMatches", [])
        result = {}
        for m in matches:
            fp = m.get("file", {}).get("fileFingerprint")
            mod_id = m.get("id")
            if fp is not None:
                result[fp] = {"mod_id": mod_id}
        return result
    except requests.exceptions.RequestException as e:
        print(f"  [!] Нет связи с CurseForge ({e}). Пропускаю этот источник.")
        return {}
    except Exception as e:
        print(f"  [!] Неожиданная ошибка CurseForge: {e}")
        return {}


def get_curseforge_mod_sides(mod_id: int):
    """CurseForge не отдаёт явный client/server статус, как Modrinth.
    Берём categories/classId как очень грубый намёк, либо просто
    подтверждаем сам факт нахождения мода (без точной стороны)."""
    if not CURSEFORGE_API_KEY:
        return None
    try:
        r = requests.get(
            f"{CURSEFORGE_API}/mods/{mod_id}",
            headers={"x-api-key": CURSEFORGE_API_KEY, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return r.json().get("data", {})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 5. Локальные метаданные внутри jar
# ---------------------------------------------------------------------------
def read_fabric_environment(jar_path: Path):
    try:
        with zipfile.ZipFile(jar_path) as z:
            if "fabric.mod.json" not in z.namelist():
                return None
            raw = z.read("fabric.mod.json").decode("utf-8", errors="ignore")
            data = json.loads(raw)
            return data.get("environment")
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, OSError):
        return None
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
    except (zipfile.BadZipFile, OSError):
        return None
    except Exception:
        return None


def classify_local(jar_path: Path) -> str:
    env = read_fabric_environment(jar_path)
    if env == "client":
        return "client-only (Fabric)"
    if env == "server":
        return "server-only (Fabric)"
    if env == "*":
        return "both, точный optional-статус неизвестен (Fabric)"

    forge = read_forge_toml(jar_path)
    if forge == "client":
        return "client-only (Forge, эвристика)"
    if forge == "server":
        return "server-only (Forge, эвристика)"

    return "неизвестно (нет данных ни в jar, ни на Modrinth/CurseForge)"


# ---------------------------------------------------------------------------
# 6. Основная логика
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print(" Mod Sorter - определение client/server модов")
    print("=" * 60)
    print()

    mods_dir = pick_mods_folder()
    print(f"Папка: {mods_dir}\n")

    try:
        jars = sorted(mods_dir.glob("*.jar"))
    except Exception as e:
        print(f"Не удалось прочитать папку: {e}")
        input("Нажми Enter для выхода...")
        sys.exit(1)

    if not jars:
        print("В папке нет .jar файлов.")
        input("Нажми Enter для выхода...")
        sys.exit(0)

    print(f"Найдено модов: {len(jars)}")
    print("Считаю хеши файлов...")

    hash_to_path = {}
    broken_files = []
    for jar in jars:
        try:
            hash_to_path[sha1_of_file(jar)] = jar
        except Exception as e:
            broken_files.append((jar.name, str(e)))

    print("Запрашиваю Modrinth...")
    modrinth_data = query_modrinth_batch(list(hash_to_path.keys()))

    results = []  # (name, category, source)
    need_curseforge = []  # jars not found on modrinth, to try CF fallback

    for h, jar in hash_to_path.items():
        version_info = modrinth_data.get(h)
        if version_info:
            project_id = version_info.get("project_id")
            sides = get_modrinth_project_sides(project_id) if project_id else None
            if sides:
                client_side, server_side = sides
                results.append((jar.name, f"client={client_side}, server={server_side}", "Modrinth", jar))
                continue
        need_curseforge.append(jar)

    if need_curseforge and CURSEFORGE_API_KEY:
        print(f"Не найдено на Modrinth: {len(need_curseforge)}. Пробую CurseForge...")
        fp_map = {}
        for jar in need_curseforge:
            try:
                fp_map[curseforge_murmur2_hash(jar)] = jar
            except Exception:
                continue
        cf_data = query_curseforge_by_fingerprint(list(fp_map.keys()))
        still_unresolved = []
        for fp, jar in fp_map.items():
            match = cf_data.get(fp)
            if match:
                results.append((jar.name, "найден на CurseForge (точная сторона недоступна через API)", "CurseForge", jar))
            else:
                still_unresolved.append(jar)
        need_curseforge = still_unresolved
    elif need_curseforge and not CURSEFORGE_API_KEY:
        print(f"Не найдено на Modrinth: {len(need_curseforge)}. CurseForge пропущен (нет API-ключа).")

    # fallback на локальные данные jar для всего, что осталось нерешённым
    for jar in need_curseforge:
        category = classify_local(jar)
        results.append((jar.name, category, "локально", jar))

    # сортируем результаты в исходном порядке по имени файла
    results.sort(key=lambda x: x[0].lower())

    # ------------------------------------------------------------------
    # Группировка
    # ------------------------------------------------------------------
    must_have_client = []
    client_optional_on_server = []
    server_only_not_needed_client = []
    unclear = []

    for name, category, source, jar in results:
        if source == "Modrinth":
            try:
                cs = category.split("client=")[1].split(",")[0]
                ss = category.split("server=")[1]
            except IndexError:
                unclear.append(name)
                continue
            if cs == "required":
                must_have_client.append(name)
            elif cs == "optional" and ss in ("required", "optional"):
                client_optional_on_server.append(name)
            elif cs == "unsupported":
                server_only_not_needed_client.append(name)
            else:
                unclear.append(name)
        elif source == "локально":
            if "client-only" in category:
                must_have_client.append(name)
            elif "server-only" in category:
                server_only_not_needed_client.append(name)
            else:
                unclear.append(name)
        else:  # CurseForge без точной стороны
            unclear.append(name)

    # ------------------------------------------------------------------
    # Вывод в файл (UTF-8, с кириллицей)
    # ------------------------------------------------------------------
    out_path = mods_dir.parent / "mod_sort_result.txt"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"Папка модов: {mods_dir}\n")
            f.write(f"Найдено модов: {len(jars)}\n\n")

            f.write(f"{'Файл':45} {'Категория':55} {'Источник'}\n")
            f.write("-" * 120 + "\n")
            for name, category, source, jar in results:
                f.write(f"{name:45} {category:55} {source}\n")

            if broken_files:
                f.write("\nНе удалось прочитать (повреждены/заняты):\n")
                for name, err in broken_files:
                    f.write(f"  - {name}: {err}\n")

            f.write("\n=== Группировка ===\n\n")
            f.write("Обязательны на клиенте:\n")
            for n in must_have_client:
                f.write(f"  - {n}\n")
            f.write("\nЕсть на сервере, клиенту не обязательны (можно не ставить):\n")
            for n in client_optional_on_server:
                f.write(f"  - {n}\n")
            f.write("\nТолько серверные (клиенту не нужны вообще):\n")
            for n in server_only_not_needed_client:
                f.write(f"  - {n}\n")
            if unclear:
                f.write("\nНеясно / нужна ручная проверка:\n")
                for n in unclear:
                    f.write(f"  - {n}\n")
        print(f"\nГотово. Отчёт сохранён: {out_path}")
    except Exception as e:
        print(f"\nНе удалось записать файл отчёта ({e}). Вывожу результат прямо в консоль:\n")
        for name, category, source, jar in results:
            print(f"{name} | {category} | {source}")

    print(f"\nОбязательны на клиенте: {len(must_have_client)}")
    print(f"Опциональны на клиенте (есть на сервере): {len(client_optional_on_server)}")
    print(f"Только серверные: {len(server_only_not_needed_client)}")
    if unclear:
        print(f"Неясно: {len(unclear)}")
    if broken_files:
        print(f"Не удалось прочитать: {len(broken_files)}")

    input("\nНажми Enter для выхода...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
    except Exception as e:
        print(f"\nНепредвиденная ошибка: {e}")
        input("Нажми Enter для выхода...")