import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModuleLoadReport:
    loaded_modules: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_user_modules(workspace_path: str = "user_workspace"):
    """
    动态扫描并加载用户工作区下的所有 Python 文件。
    类似于 freqtrade 扫描 user_data 文件夹的方式。
    一旦被 import，文件中的 @MinerRegistry.register 等装饰器就会自动触发，将用户的类和算子注入到系统的注册中心。
    """
    workspace_dir = Path(workspace_path).absolute()
    report = ModuleLoadReport()
    if not workspace_dir.exists():
        # S-02: a first-run install without a user workspace is valid. It was
        # recorded as a fatal load error and blocked an otherwise legal launch.
        message = f"User workspace directory {workspace_dir} not found; skipping plugins."
        logger.warning(message)
        report.warnings.append(message)
        return report

    # 定义我们要扫描的核心用户子目录
    target_dirs = ["custom_miners", "custom_operators", "custom_fitness"]

    loaded_count = 0
    for target in target_dirs:
        target_path = workspace_dir / target
        if not target_path.exists() or not target_path.is_dir():
            continue

        # S-01: deterministic order + collision-free module names. The old
        # ``<dir>.<stem>`` name let two files with the same stem overwrite each
        # other and made registration order depend on filesystem order.
        for file_path in sorted(target_path.rglob("*.py")):
            if file_path.name == "__init__.py":
                continue

            relative = file_path.relative_to(workspace_dir).with_suffix("")
            module_name = "factorminer_user." + ".".join(relative.parts)
            try:
                spec = importlib.util.spec_from_file_location(module_name, str(file_path))
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    # Register before exec so decorators / dataclasses / pickle
                    # can resolve ``sys.modules[__name__]`` like a normal import.
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    loaded_count += 1
                    report.loaded_modules.append(module_name)
                    logger.debug("Successfully loaded user module: %s from %s",
                                 module_name, file_path)
            except (Exception, SystemExit) as e:
                # S-01: ``SystemExit`` / ``os._exit``-style plugin code must not
                # kill the host process. SystemExit is a BaseException and used
                # to sail straight past ``except Exception``.
                sys.modules.pop(module_name, None)
                message = f"Failed to load user module {file_path}: {type(e).__name__}: {e}"
                logger.error(message)
                report.errors.append(message)

    logger.info("Dynamic loader successfully imported %d user modules from %s.",
                loaded_count, workspace_path)
    return report
