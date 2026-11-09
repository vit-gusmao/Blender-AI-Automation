"""Render state ownership, independent of bpy for focused unit tests."""
from dataclasses import dataclass
from pathlib import Path
import time


@dataclass
class PendingRender:
    command_id: str
    scene: object
    filepath: str
    animation: bool
    started: float
    render_values: dict
    camera: object
    frame: int
    outputs: tuple

    @classmethod
    def capture(cls, command_id, scene, filepath, animation):
        keys = ("resolution_x", "resolution_y", "resolution_percentage",
                "filepath", "use_file_extension")
        return cls(command_id, scene, filepath, animation, time.time(),
                   {key: getattr(scene.render, key) for key in keys},
                   scene.camera, scene.frame_current, (filepath,))

    def restore(self):
        for key, value in self.render_values.items():
            setattr(self.scene.render, key, value)
        self.scene.camera = self.camera
        if hasattr(self.scene, "frame_set"):
            self.scene.frame_set(self.frame)
        else:
            self.scene.frame_current = self.frame

    def missing_outputs(self):
        return [path for path in self.outputs if not Path(path).is_file()]
