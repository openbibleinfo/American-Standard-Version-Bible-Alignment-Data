"""Where a runner keeps requests, receipts and responses.

FileStore is the research layout: one directory per request under the run
directory. MemoryStore keeps the same records in process memory for the web
service, which writes nothing to disk. Both address a record by
(directory, filename) and hold the same bytes.
"""
import json
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


class FileStore:
    def exists(self, directory, name):
        return (directory / name).exists()

    def read_bytes(self, directory, name):
        return (directory / name).read_bytes()

    def read(self, directory, name):
        return json.loads(self.read_bytes(directory, name))

    def write_text(self, directory, name, text, exclusive=False):
        directory.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents silently repeating a potentially billed request.
        with (directory / name).open('x' if exclusive else 'w') as handle:
            handle.write(text)

    def write(self, directory, name, value):
        self.write_text(directory, name, dumps(value))

    def has_directory(self, directory):
        return directory.exists()

    def rename(self, source, target):
        source.rename(target)


class MemoryStore:
    def __init__(self):
        self.records = {}

    def _directory(self, directory):
        return self.records.setdefault(str(directory), {})

    def exists(self, directory, name):
        return name in self.records.get(str(directory), {})

    def read_bytes(self, directory, name):
        try:
            return self.records[str(directory)][name]
        except KeyError:
            raise FileNotFoundError(str(Path(directory) / name)) from None

    def read(self, directory, name):
        return json.loads(self.read_bytes(directory, name))

    def write_text(self, directory, name, text, exclusive=False):
        records = self._directory(directory)
        if exclusive and name in records:
            raise FileExistsError(str(Path(directory) / name))
        records[name] = text.encode()

    def write(self, directory, name, value):
        self.write_text(directory, name, dumps(value))

    def has_directory(self, directory):
        return str(directory) in self.records

    def rename(self, source, target):
        if str(target) in self.records:
            raise FileExistsError(str(target))
        self.records[str(target)] = self.records.pop(str(source))
