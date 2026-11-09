"""Unit: workspace externo — confinamento e limpeza (§8.1)."""
from __future__ import annotations

import os
import subprocess

import pytest

from external_runtime.workspace_manager import WorkspaceManager


def test_create_task_dir_inside_root(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    task = ws.create_task_dir("job")
    assert task.is_dir()
    assert task.is_relative_to(ws.root)


def test_confine_rejects_escape(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    with pytest.raises(PermissionError):
        ws.confine("../outside.txt")
    with pytest.raises(PermissionError):
        ws.confine(tmp_path / "elsewhere" / "x.txt")
    ok = ws.confine("sub/file.txt")
    assert ok == ws.root / "sub" / "file.txt"


@pytest.mark.parametrize("prefix", ["", ".", "..", "../job", "..\\job",
                                     "nested/job", "nested\\job", "/absolute"])
def test_create_task_dir_rejects_unsafe_prefix(tmp_path, prefix):
    ws = WorkspaceManager(tmp_path / "ws")
    with pytest.raises(ValueError):
        ws.create_task_dir(prefix)


def test_resolve_existing_file_rejects_escape_and_non_files(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    inside = ws.root / "inside.py"
    inside.write_text("print('inside')", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')", encoding="utf-8")

    assert ws.resolve_existing_file("inside.py") == inside.resolve()
    assert ws.resolve_existing_file(inside) == inside.resolve()
    with pytest.raises(PermissionError):
        ws.resolve_existing_file(outside)
    with pytest.raises(PermissionError):
        ws.resolve_existing_file("../outside.py")
    with pytest.raises(FileNotFoundError):
        ws.resolve_existing_file("missing.py")
    with pytest.raises(IsADirectoryError):
        ws.resolve_existing_file(ws.root)


def test_resolve_existing_file_rejects_directory_link_escape(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.py").write_text("print('escaped')", encoding="utf-8")
    link = ws.root / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation unavailable")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if created.returncode != 0:
            pytest.skip("directory link creation unavailable")

    with pytest.raises(PermissionError):
        ws.resolve_existing_file(link / "escaped.py")


def test_cleanup_preserves_leased_task_until_release(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    active = ws.create_task_dir("active")
    removable = ws.create_task_dir("old")

    with ws.task_lease(active):
        assert active in ws.active_task_dirs
        assert ws.cleanup(keep_last=0) == 1
        assert active.is_dir()
        assert not removable.exists()

    assert active not in ws.active_task_dirs
    assert ws.cleanup(keep_last=0) == 1
    assert not active.exists()


def test_cleanup_other_manager_preserves_child_lease(tmp_path):
    first = WorkspaceManager(tmp_path / "ws")
    second = WorkspaceManager(tmp_path / "ws")
    task = first.create_task_dir("shared")
    child = task / "nested"
    child.mkdir()

    with first.task_lease(child):
        assert second.cleanup(keep_last=0) == 0
        assert task.is_dir()

    assert second.cleanup(keep_last=0) == 1
    assert not task.exists()


def test_nested_leases_keep_task_locked_until_outer_release(tmp_path):
    first = WorkspaceManager(tmp_path / "ws")
    second = WorkspaceManager(tmp_path / "ws")
    task = first.create_task_dir("nested")
    child = task / "child"
    child.mkdir()

    with first.task_lease(task):
        with first.task_lease(child):
            assert second.cleanup(keep_last=0) == 0
        assert second.cleanup(keep_last=0) == 0
        assert task.is_dir()

    assert second.cleanup(keep_last=0) == 1


def test_list_and_cleanup(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    for _ in range(5):
        ws.create_task_dir("t")
    ws.cleanup(keep_last=2)
    remaining = [d for d in ws.root.iterdir() if d.is_dir()]
    assert len(remaining) == 2
