from pathlib import Path


class Reader:
    """Reads a file through its grammar."""

    @property
    def name(self) -> str:
        return self.path.stem

    def for_file(self, path: str | None) -> Config:
        # a pipe has no name
        if path is None:
            return self.default
        return self.configs[Path(path).suffix]


def read(path: str) -> Reader:
    return Reader(Path(path))
