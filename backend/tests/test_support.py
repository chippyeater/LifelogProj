import importlib
import shutil
import sys
import types
from pathlib import Path


# 统一测试根目录，所有临时文件都放在项目内，避免系统临时目录权限问题
REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = REPO_ROOT / "tests" / "_tmp"


def prepare_temp_dir(name: str) -> Path:
    path = TEST_TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_temp_root() -> None:
    if TEST_TMP_ROOT.exists():
        shutil.rmtree(TEST_TMP_ROOT)


def import_with_stubs(module_name: str, stub_builders: dict[str, object]):
    # 在导入目标模块前注入最小桩模块，避免重依赖拖慢测试
    for key, value in stub_builders.items():
        module = types.ModuleType(key)
        for attr_name, attr_value in value.items():
            setattr(module, attr_name, attr_value)
        sys.modules[key] = module

    if module_name in sys.modules:
        del sys.modules[module_name]
    return importlib.import_module(module_name)
