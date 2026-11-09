from types import SimpleNamespace
from universal_mcp.render_state import PendingRender


def test_render_snapshot_restores_every_temporary_field():
    render = SimpleNamespace(resolution_x=100, resolution_y=80,
                             resolution_percentage=70, filepath="old",
                             use_file_extension=False)
    scene = SimpleNamespace(render=render, camera=object(), frame_current=4)
    camera = scene.camera
    pending = PendingRender.capture("cmd", scene, "output.png", False)
    render.resolution_x, render.resolution_y = 9, 8
    render.resolution_percentage, render.filepath = 10, "new"
    render.use_file_extension, scene.camera = True, None
    scene.frame_current = 9
    pending.restore()
    assert (render.resolution_x, render.resolution_y, render.resolution_percentage) == (100, 80, 70)
    assert render.filepath == "old" and not render.use_file_extension
    assert scene.camera is camera and scene.frame_current == 4
