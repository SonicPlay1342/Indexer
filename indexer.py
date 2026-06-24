import os
import sys
import sqlite3
import hashlib
import argparse
import time
from datetime import datetime
from pathlib import Path


DB_NAME = "index.db"
CHUNK_SIZE = 65536  # 64 KB для чтения при вычислении хэша


class Indexer:
    def __init__(self, db_path=DB_NAME):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Создаёт таблицы, если их нет."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_path TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    size INTEGER,
                    mtime REAL,
                    hash TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    deleted INTEGER DEFAULT 0,
                    UNIQUE(root_path, rel_path)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_root_rel ON files(root_path, rel_path)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_hash ON files(hash)
            """)
            conn.commit()

    def _compute_hash(self, file_path):
        """Вычисляет SHA-256 хэш файла."""
        sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except (IOError, OSError):
            return None

    def scan(self, root_path):
        """
        Сканирует папку root_path и обновляет индекс.
        Возвращает словарь со статистикой.
        """
        root_path = os.path.abspath(root_path)
        if not os.path.isdir(root_path):
            print(f"Ошибка: '{root_path}' не является папкой.")
            return None

        now = time.time()
        stats = {"total": 0, "new": 0, "updated": 0, "errors": 0}

        # 1. Собираем информацию о файлах
        file_list = []
        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, root_path)
                try:
                    stat = os.stat(full_path)
                    size = stat.st_size
                    mtime = stat.st_mtime
                    # Для больших файлов можно пропустить хэш, но мы вычисляем всегда
                    file_hash = self._compute_hash(full_path)
                    file_list.append((root_path, rel_path, size, mtime, file_hash))
                except (OSError, PermissionError) as e:
                    stats["errors"] += 1
                    print(f"Не удалось прочитать {full_path}: {e}")

        stats["total"] = len(file_list)

        # 2. Обновляем БД в транзакции
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Помечаем все существующие записи для этого root_path как удалённые (временно)
            # Мы их обновим позже
            cursor.execute(
                "UPDATE files SET deleted = 1 WHERE root_path = ?",
                (root_path,)
            )

            for root, rel, size, mtime, file_hash in file_list:
                cursor.execute(
                    """
                    INSERT INTO files (root_path, rel_path, size, mtime, hash, first_seen, last_seen, deleted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(root_path, rel_path) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        hash = excluded.hash,
                        last_seen = excluded.last_seen,
                        deleted = 0
                    """,
                    (root, rel, size, mtime, file_hash, now, now)
                )
                # Проверяем, была ли запись новой или обновлённой
                if cursor.rowcount == 1:  # вставка новой
                    stats["new"] += 1
                else:
                    stats["updated"] += 1

            # Оставляем записи, которые остались deleted=1 (они реально удалены)
            # Но не удаляем их, чтобы сохранить историю
            conn.commit()

        # Убираем из статистики дублирование (rowcount не совсем точен для обновлений, но приблизительно)
        # Лучше пересчитать
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM files WHERE root_path = ? AND deleted = 0",
                (root_path,)
            )
            active = cursor.fetchone()[0]
            # Статистика: новые - это те, у которых first_seen == last_seen и не deleted? 
            # Мы просто выведем общее количество активных
            stats["active"] = active

        print(f"Сканирование завершено. Всего файлов: {stats['total']}, "
              f"активных в индексе: {stats['active']}, ошибок: {stats['errors']}")
        return stats

    def find_duplicates(self, root_path=None):
        """
        Находит дубликаты файлов на основе хэша.
        Если root_path указан, ищет только в этой папке, иначе по всей БД.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = """
                SELECT root_path, rel_path, size, hash
                FROM files
                WHERE deleted = 0 AND hash IS NOT NULL
            """
            params = []
            if root_path:
                query += " AND root_path = ?"
                params.append(root_path)
            query += " ORDER BY hash, size"
            cursor.execute(query, params)
            rows = cursor.fetchall()

        # Группируем по хэшу
        groups = {}
        for root, rel, size, h in rows:
            groups.setdefault(h, []).append((root, rel, size))

        duplicates = {h: files for h, files in groups.items() if len(files) > 1}
        if not duplicates:
            print("Дубликатов не найдено.")
            return

        print(f"Найдено {len(duplicates)} групп дубликатов:")
        for h, files in duplicates.items():
            print(f"\nХэш: {h} (размер: {files[0][2]} байт)")
            for root, rel, _ in files:
                print(f"  - {os.path.join(root, rel)}")

    def compare_backup(self, source, backup):
        """
        Сравнивает две папки: source (оригинал) и backup (резервная копия).
        Выводит отсутствующие, изменённые и лишние файлы.
        """
        source = os.path.abspath(source)
        backup = os.path.abspath(backup)

        if not os.path.isdir(source):
            print(f"Ошибка: исходная папка '{source}' не существует.")
            return
        if not os.path.isdir(backup):
            print(f"Ошибка: папка резерва '{backup}' не существует.")
            return

        # Сканируем обе папки без записи в БД (просто собираем словари)
        def scan_folder(folder):
            result = {}
            for dirpath, _, filenames in os.walk(folder):
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, folder)
                    try:
                        stat = os.stat(full)
                        result[rel] = (stat.st_size, stat.st_mtime)
                    except OSError:
                        pass
            return result

        print("Сканирование исходной папки...")
        source_files = scan_folder(source)
        print("Сканирование резервной папки...")
        backup_files = scan_folder(backup)

        source_set = set(source_files.keys())
        backup_set = set(backup_files.keys())

        missing = source_set - backup_set
        extra = backup_set - source_set
        common = source_set & backup_set

        changed = []
        for rel in common:
            s_size, s_mtime = source_files[rel]
            b_size, b_mtime = backup_files[rel]
            if s_size != b_size or abs(s_mtime - b_mtime) > 1:  # допуск 1 секунда
                changed.append(rel)

        print("\n=== Результат сравнения ===")
        print(f"Файлов в источнике: {len(source_files)}")
        print(f"Файлов в резерве:   {len(backup_files)}")
        print(f"Общих файлов:       {len(common)}")

        if missing:
            print(f"\nОтсутствуют в резерве ({len(missing)}):")
            for rel in sorted(missing):
                print(f"  - {rel}")
        else:
            print("\nВсе файлы источника присутствуют в резерве.")

        if changed:
            print(f"\nИзменены (размер или дата) ({len(changed)}):")
            for rel in sorted(changed):
                print(f"  - {rel}")
        else:
            print("\nВсе общие файлы идентичны.")

        if extra:
            print(f"\nЛишние файлы в резерве ({len(extra)}):")
            for rel in sorted(extra):
                print(f"  - {rel}")
        else:
            print("\nВ резерве нет лишних файлов.")

    def show_changes(self, root_path=None):
        """
        Показывает файлы, добавленные, изменённые или удалённые
        с момента последнего сканирования (для указанной папки или всех).
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Для каждого root_path определяем последнее сканирование (max last_seen)
            # Но проще показать все файлы, у которых first_seen == last_seen (новые)
            # и файлы, у которых deleted = 1 (удалены)
            query = """
                SELECT root_path, rel_path, size, first_seen, last_seen, deleted
                FROM files
                WHERE 1=1
            """
            params = []
            if root_path:
                query += " AND root_path = ?"
                params.append(root_path)
            cursor.execute(query, params)
            rows = cursor.fetchall()

        new_files = []
        changed_files = []
        deleted_files = []

        for root, rel, size, first, last, deleted in rows:
            if deleted:
                deleted_files.append((root, rel))
            elif first == last:
                new_files.append((root, rel, size))
            else:
                # Изменённые: проверим, не изменился ли размер или mtime? 
                # Но мы не храним историю старых значений, поэтому считаем изменёнными,
                # если first_seen != last_seen, но не новые
                changed_files.append((root, rel, size))

        if not any([new_files, changed_files, deleted_files]):
            print("Изменений не обнаружено (или индекс пуст).")
            return

        if new_files:
            print(f"\nНовые файлы ({len(new_files)}):")
            for root, rel, size in new_files[:20]:  # ограничим вывод
                print(f"  - {os.path.join(root, rel)} ({size} байт)")
            if len(new_files) > 20:
                print(f"  ... и ещё {len(new_files)-20}")

        if changed_files:
            print(f"\nИзменённые файлы ({len(changed_files)}):")
            for root, rel, size in changed_files[:20]:
                print(f"  - {os.path.join(root, rel)} ({size} байт)")
            if len(changed_files) > 20:
                print(f"  ... и ещё {len(changed_files)-20}")

        if deleted_files:
            print(f"\nУдалённые файлы ({len(deleted_files)}):")
            for root, rel in deleted_files[:20]:
                print(f"  - {os.path.join(root, rel)}")
            if len(deleted_files) > 20:
                print(f"  ... и ещё {len(deleted_files)-20}")

    def status(self, root_path=None):
        """Показывает общую статистику по индексу."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if root_path:
                cursor.execute(
                    "SELECT COUNT(*) FROM files WHERE root_path = ? AND deleted = 0",
                    (root_path,)
                )
                active = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM files WHERE root_path = ? AND deleted = 1",
                    (root_path,)
                )
                deleted = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT SUM(size) FROM files WHERE root_path = ? AND deleted = 0",
                    (root_path,)
                )
                total_size = cursor.fetchone()[0] or 0
                print(f"Статистика для '{root_path}':")
                print(f"  Активных файлов: {active}")
                print(f"  Удалённых файлов (в истории): {deleted}")
                print(f"  Общий размер: {total_size} байт ({total_size/1024/1024:.2f} МБ)")
            else:
                cursor.execute("SELECT COUNT(*) FROM files WHERE deleted = 0")
                active = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM files WHERE deleted = 1")
                deleted = cursor.fetchone()[0]
                cursor.execute("SELECT SUM(size) FROM files WHERE deleted = 0")
                total_size = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(DISTINCT root_path) FROM files")
                roots = cursor.fetchone()[0]
                print("Общая статистика:")
                print(f"  Отслеживаемых папок: {roots}")
                print(f"  Активных файлов: {active}")
                print(f"  Удалённых файлов (в истории): {deleted}")
                print(f"  Общий размер активных: {total_size} байт ({total_size/1024/1024:.2f} МБ)")


def main():
    parser = argparse.ArgumentParser(
        description="Консольный индексатор папок с поиском дубликатов и сравнением резервных копий."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Доступные команды")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Просканировать папку и обновить индекс")
    scan_parser.add_argument("path", help="Путь к папке для сканирования")

    # duplicates
    dup_parser = subparsers.add_parser("duplicates", help="Найти дубликаты файлов")
    dup_parser.add_argument("--path", help="Ограничить поиск указанной папкой", default=None)

    # compare
    comp_parser = subparsers.add_parser("compare", help="Сравнить две папки (источник и резерв)")
    comp_parser.add_argument("source", help="Путь к исходной папке")
    comp_parser.add_argument("backup", help="Путь к папке резервной копии")

    # changes
    changes_parser = subparsers.add_parser("changes", help="Показать изменения с последнего сканирования")
    changes_parser.add_argument("--path", help="Ограничить указанной папкой", default=None)

    # status
    status_parser = subparsers.add_parser("status", help="Показать статистику индекса")
    status_parser.add_argument("--path", help="Статистика для конкретной папки", default=None)

    args = parser.parse_args()
    indexer = Indexer()

    if args.command == "scan":
        indexer.scan(args.path)
    elif args.command == "duplicates":
        indexer.find_duplicates(args.path)
    elif args.command == "compare":
        indexer.compare_backup(args.source, args.backup)
    elif args.command == "changes":
        indexer.show_changes(args.path)
    elif args.command == "status":
        indexer.status(args.path)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()