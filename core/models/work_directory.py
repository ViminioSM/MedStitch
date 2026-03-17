class WorkDirectory:
    """Model for holding Working Directory Information."""

    def __init__(
        self,
        input: str,
        output: str,
        postprocess: str,
    ) -> None:
        self.input_path: str = input
        self.output_path: str = output
        self.postprocess_path: str = postprocess
        self.input_files: list[str] = []
        self.output_files: list[str] = []

    def __repr__(self) -> str:
        parts = [f"input_path={self.input_path!r}"]
        if self.input_files:
            parts.append(f"input_files={len(self.input_files)}")
        parts.append(f"output_path={self.output_path!r}")
        if self.output_files:
            parts.append(f"output_files={len(self.output_files)}")
        parts.append(f"postprocess_path={self.postprocess_path!r}")
        return f"WorkDirectory({', '.join(parts)})"
