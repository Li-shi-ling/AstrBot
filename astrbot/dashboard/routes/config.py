import asyncio
import copy
import inspect
import os
import traceback
from pathlib import Path
from typing import Any

from quart import request

from astrbot.core import astrbot_config, file_token_service, logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.config.default import (
    CONFIG_METADATA_2,
    CONFIG_METADATA_3,
    CONFIG_METADATA_3_SYSTEM,
    DEFAULT_CONFIG,
    DEFAULT_VALUE_MAP,
)
from astrbot.core.config.i18n_utils import ConfigMetadataI18n
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.platform.register import platform_cls_map, platform_registry
from astrbot.core.provider import Provider
from astrbot.core.provider.register import provider_registry
from astrbot.core.star.star import StarMetadata, star_registry
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
)
from astrbot.core.utils.llm_metadata import LLM_METADATAS
from astrbot.core.utils.webhook_utils import ensure_platform_webhook_config

from .route import Response, Route, RouteContext
from .util import (
    config_key_to_folder,
    get_schema_item,
    normalize_rel_path,
    sanitize_filename,
)

MAX_FILE_BYTES = 500 * 1024 * 1024


def try_cast(value: Any, type_: str):
    if type_ == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif (
        type_ == "float"
        and isinstance(value, str)
        and value.replace(".", "", 1).isdigit()
    ) or (type_ == "float" and isinstance(value, int)):
        return float(value)
    elif type_ == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None


def _expect_type(value, expected_type, path_key, errors, expected_name=None) -> bool:
    if not isinstance(value, expected_type):
        errors.append(
            f"閿欒鐨勭被鍨?{path_key}: 鏈熸湜鏄?{expected_name or expected_type.__name__}, "
            f"寰楀埌浜?{type(value).__name__}"
        )
        return False
    return True


def _validate_template_list(value, meta, path_key, errors, validate_fn) -> None:
    if not _expect_type(value, list, path_key, errors, "list"):
        return

    templates = meta.get("templates")
    if not isinstance(templates, dict):
        templates = {}

    for idx, item in enumerate(value):
        item_path = f"{path_key}[{idx}]"
        if not _expect_type(item, dict, item_path, errors, "dict"):
            continue

        template_key = item.get("__template_key") or item.get("template")
        if not template_key:
            errors.append(f"缂哄皯妯℃澘閫夋嫨 {item_path}: 闇€瑕?__template_key")
            continue

        template_meta = templates.get(template_key)
        if not template_meta:
            errors.append(f"鏈煡妯℃澘 {item_path}: {template_key}")
            continue

        validate_fn(
            item,
            template_meta.get("items", {}),
            path=f"{item_path}.",
        )


def validate_config(data, schema: dict, is_core: bool) -> tuple[list[str], dict]:
    errors = []

    def validate(data: dict, metadata: dict = schema, path="") -> None:
        for key, value in data.items():
            if key not in metadata:
                continue
            meta = metadata[key]
            if "type" not in meta:
                logger.debug(f"閰嶇疆椤?{path}{key} 娌℃湁绫诲瀷瀹氫箟, 璺宠繃鏍￠獙")
                continue
            # null 杞崲
            if value is None:
                data[key] = DEFAULT_VALUE_MAP[meta["type"]]
                continue

            if meta["type"] == "template_list":
                _validate_template_list(value, meta, f"{path}{key}", errors, validate)
                continue

            if meta["type"] == "file":
                if not _expect_type(value, list, f"{path}{key}", errors, "list"):
                    continue
                for idx, item in enumerate(value):
                    if not isinstance(item, str):
                        errors.append(
                            f"Invalid type {path}{key}[{idx}]: expected string, got {type(item).__name__}",
                        )
                        continue
                    normalized = normalize_rel_path(item)
                    if not normalized or not normalized.startswith("files/"):
                        errors.append(
                            f"Invalid file path {path}{key}[{idx}]: {item}",
                        )
                        continue
                    key_path = f"{path}{key}"
                    expected_folder = config_key_to_folder(key_path)
                    expected_prefix = f"files/{expected_folder}/"
                    if not normalized.startswith(expected_prefix):
                        errors.append(
                            f"Invalid file path {path}{key}[{idx}]: {item}",
                        )
                        continue
                    value[idx] = normalized
                continue

            if meta["type"] == "list" and not isinstance(value, list):
                errors.append(
                    f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?list, 寰楀埌浜?{type(value).__name__}",
                )
            elif (
                meta["type"] == "list"
                and isinstance(value, list)
                and value
                and "items" in meta
                and isinstance(value[0], dict)
            ):
                # 褰撳墠浠呴拡瀵?list[dict] 鐨勬儏鍐佃繘琛岀被鍨嬫牎楠岋紝浠ラ€傞厤 AstrBot 涓?platform銆乸rovider 鐨勯厤缃?
                for item in value:
                    validate(item, meta["items"], path=f"{path}{key}.")
            elif meta["type"] == "object" and isinstance(value, dict):
                validate(value, meta["items"], path=f"{path}{key}.")

            if meta["type"] == "int" and not isinstance(value, int):
                casted = try_cast(value, "int")
                if casted is None:
                    errors.append(
                        f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?int, 寰楀埌浜?{type(value).__name__}",
                    )
                data[key] = casted
            elif meta["type"] == "float" and not isinstance(value, float):
                casted = try_cast(value, "float")
                if casted is None:
                    errors.append(
                        f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?float, 寰楀埌浜?{type(value).__name__}",
                    )
                data[key] = casted
            elif meta["type"] == "bool" and not isinstance(value, bool):
                errors.append(
                    f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?bool, 寰楀埌浜?{type(value).__name__}",
                )
            elif meta["type"] in ["string", "text"] and not isinstance(value, str):
                errors.append(
                    f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?string, 寰楀埌浜?{type(value).__name__}",
                )
            elif meta["type"] == "list" and not isinstance(value, list):
                errors.append(
                    f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?list, 寰楀埌浜?{type(value).__name__}",
                )
            elif meta["type"] == "object" and not isinstance(value, dict):
                errors.append(
                    f"閿欒鐨勭被鍨?{path}{key}: 鏈熸湜鏄?dict, 寰楀埌浜?{type(value).__name__}",
                )

    if is_core:
        meta_all = {
            **schema["platform_group"]["metadata"],
            **schema["provider_group"]["metadata"],
            **schema["misc_config_group"]["metadata"],
        }
        validate(data, meta_all)
    else:
        validate(data, schema)

    return errors, data


def _log_computer_config_changes(old_config: dict, new_config: dict) -> None:
    """Compare and log Computer/sandbox configuration changes."""
    old_ps = old_config.get("provider_settings", {})
    new_ps = new_config.get("provider_settings", {})

    # Check computer_use_runtime
    old_runtime = old_ps.get("computer_use_runtime", "none")
    new_runtime = new_ps.get("computer_use_runtime", "none")
    if old_runtime != new_runtime:
        logger.info(
            "[Computer] Config changed: computer_use_runtime %s -> %s",
            old_runtime,
            new_runtime,
        )

    # Check sandbox sub-keys
    old_sandbox = old_ps.get("sandbox", {})
    new_sandbox = new_ps.get("sandbox", {})
    all_keys = set(old_sandbox.keys()) | set(new_sandbox.keys())
    for key in sorted(all_keys):
        old_val = old_sandbox.get(key)
        new_val = new_sandbox.get(key)
        if old_val != new_val:
            # Mask tokens/secrets in log output
            if "token" in key or "secret" in key:
                old_display = "***" if old_val else "(empty)"
                new_display = "***" if new_val else "(empty)"
            else:
                old_display = old_val
                new_display = new_val
            logger.info(
                "[Computer] Config changed: sandbox.%s %s -> %s",
                key,
                old_display,
                new_display,
            )


async def _validate_neo_connectivity(
    post_config: dict,
) -> str | None:
    """Check if Bay is reachable when Shipyard Neo sandbox is configured.

    Returns a warning message string if Bay isn't reachable, or None if
    everything looks fine (or Neo isn't configured).
    """
    ps = post_config.get("provider_settings", {})
    runtime = ps.get("computer_use_runtime", "none")
    sandbox = ps.get("sandbox", {})
    booter = sandbox.get("booter", "")

    # Only check when sandbox mode + shipyard_neo is selected
    if runtime != "sandbox" or booter != "shipyard_neo":
        return None

    endpoint = sandbox.get("shipyard_neo_endpoint", "").rstrip("/")
    if not endpoint:
        return "鈿狅笍 Shipyard Neo endpoint 鏈缃?

    access_token = sandbox.get("shipyard_neo_access_token", "")
    if not access_token:
        # Try auto-discovery
        from astrbot.core.computer.computer_client import _discover_bay_credentials

        access_token = _discover_bay_credentials(endpoint)

    if not access_token:
        return (
            "鈿狅笍 鏈壘鍒?Bay API Key銆傝濉啓璁块棶浠ょ墝锛?
            "鎴栫‘淇?Bay 鐨?credentials.json 鍙鑷姩鍙戠幇銆?
        )

    # Connectivity check
    import aiohttp

    health_url = f"{endpoint}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                health_url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return (
                        f"鈿狅笍 Bay 鍋ュ悍妫€鏌ュけ璐?(HTTP {resp.status})锛?
                        f"璇风‘璁?Bay 姝ｅ湪杩愯: {endpoint}"
                    )
    except Exception:
        return f"鈿狅笍 鏃犳硶杩炴帴 Bay ({endpoint})锛岃纭 Bay 宸插惎鍔ㄣ€?

    return None


def save_config(
    post_config: dict, config: AstrBotConfig, is_core: bool = False
) -> None:
    """楠岃瘉骞朵繚瀛橀厤缃?""
    errors = None
    logger.info(f"Saving config, is_core={is_core}")

    # Snapshot old Computer config for change detection
    if is_core:
        _log_computer_config_changes(dict(config), post_config)

    try:
        if is_core:
            errors, post_config = validate_config(
                post_config,
                CONFIG_METADATA_2,
                is_core,
            )
        else:
            errors, post_config = validate_config(
                post_config, getattr(config, "schema", {}), is_core
            )
    except BaseException as e:
        logger.error(traceback.format_exc())
        logger.warning(f"楠岃瘉閰嶇疆鏃跺嚭鐜板紓甯? {e}")
        raise ValueError(f"楠岃瘉閰嶇疆鏃跺嚭鐜板紓甯? {e}")
    if errors:
        raise ValueError(f"鏍煎紡鏍￠獙鏈€氳繃: {errors}")

    config.save_config(post_config)


class ConfigRoute(Route):
    def __init__(
        self,
        context: RouteContext,
        core_lifecycle: AstrBotCoreLifecycle,
    ) -> None:
        super().__init__(context)
        self.core_lifecycle = core_lifecycle
        self.config: AstrBotConfig = core_lifecycle.astrbot_config
        self._logo_token_cache = {}  # 缂撳瓨logo token锛岄伩鍏嶉噸澶嶆敞鍐?
        self.acm = core_lifecycle.astrbot_config_mgr
        self.ucr = core_lifecycle.umop_config_router
        self.routes = {
            "/config/abconf/new": ("POST", self.create_abconf),
            "/config/abconf": ("GET", self.get_abconf),
            "/config/abconfs": ("GET", self.get_abconf_list),
            "/config/abconf/delete": ("POST", self.delete_abconf),
            "/config/abconf/update": ("POST", self.update_abconf),
            "/config/umo_abconf_routes": ("GET", self.get_uc_table),
            "/config/umo_abconf_route/update_all": ("POST", self.update_ucr_all),
            "/config/umo_abconf_route/update": ("POST", self.update_ucr),
            "/config/umo_abconf_route/delete": ("POST", self.delete_ucr),
            "/config/get": ("GET", self.get_configs),
            "/config/default": ("GET", self.get_default_config),
            "/config/astrbot/update": ("POST", self.post_astrbot_configs),
            "/config/plugin/update": ("POST", self.post_plugin_configs),
            "/config/file/upload": ("POST", self.upload_config_file),
            "/config/file/delete": ("POST", self.delete_config_file),
            "/config/file/get": ("GET", self.get_config_file_list),
            "/config/platform/new": ("POST", self.post_new_platform),
            "/config/platform/update": ("POST", self.post_update_platform),
            "/config/platform/delete": ("POST", self.post_delete_platform),
            "/config/platform/list": ("GET", self.get_platform_list),
            "/config/provider/new": ("POST", self.post_new_provider),
            "/config/provider/update": ("POST", self.post_update_provider),
            "/config/provider/delete": ("POST", self.post_delete_provider),
            "/config/provider/template": ("GET", self.get_provider_template),
            "/config/provider/check_one": ("GET", self.check_one_provider_status),
            "/config/provider/list": ("GET", self.get_provider_config_list),
            "/config/provider/model_list": ("GET", self.get_provider_model_list),
            "/config/provider/get_embedding_dim": ("POST", self.get_embedding_dim),
            "/config/provider_sources/models": (
                "GET",
                self.get_provider_source_models,
            ),
            "/config/provider_sources/update": (
                "POST",
                self.update_provider_source,
            ),
            "/config/provider_sources/delete": (
                "POST",
                self.delete_provider_source,
            ),
        }
        self.register_routes()

    async def delete_provider_source(self):
        """鍒犻櫎 provider_source锛屽苟鏇存柊鍏宠仈鐨?providers"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        provider_source_id = post_data.get("id")
        if not provider_source_id:
            return Response().error("缂哄皯 provider_source_id").__dict__

        provider_sources = self.config.get("provider_sources", [])
        target_idx = next(
            (
                i
                for i, ps in enumerate(provider_sources)
                if ps.get("id") == provider_source_id
            ),
            -1,
        )

        if target_idx == -1:
            return Response().error("鏈壘鍒板搴旂殑 provider source").__dict__

        # 鍒犻櫎 provider_source
        del provider_sources[target_idx]

        # 鍐欏洖閰嶇疆
        self.config["provider_sources"] = provider_sources

        # 鍒犻櫎寮曠敤浜嗚 provider_source 鐨?providers
        await self.core_lifecycle.provider_manager.delete_provider(
            provider_source_id=provider_source_id
        )

        try:
            save_config(self.config, self.config, is_core=True)
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(str(e)).__dict__

        return Response().ok(message="鍒犻櫎 provider source 鎴愬姛").__dict__

    async def update_provider_source(self):
        """鏇存柊鎴栨柊澧?provider_source锛屽苟閲嶈浇鍏宠仈鐨?providers"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        new_source_config = post_data.get("config") or post_data
        original_id = post_data.get("original_id")
        if not original_id:
            return Response().error("缂哄皯 original_id").__dict__

        if not isinstance(new_source_config, dict):
            return Response().error("缂哄皯鎴栭敊璇殑閰嶇疆鏁版嵁").__dict__

        # 纭繚閰嶇疆涓湁 id 瀛楁
        if not new_source_config.get("id"):
            new_source_config["id"] = original_id

        provider_sources = self.config.get("provider_sources", [])

        for ps in provider_sources:
            if ps.get("id") == new_source_config["id"] and ps.get("id") != original_id:
                return (
                    Response()
                    .error(
                        f"Provider source ID '{new_source_config['id']}' exists already, please try another ID.",
                    )
                    .__dict__
                )

        # 鏌ユ壘鏃х殑 provider_source锛岃嫢涓嶅瓨鍦ㄥ垯杩藉姞涓烘柊閰嶇疆
        target_idx = next(
            (i for i, ps in enumerate(provider_sources) if ps.get("id") == original_id),
            -1,
        )

        old_id = original_id
        if target_idx == -1:
            provider_sources.append(new_source_config)
        else:
            old_id = provider_sources[target_idx].get("id")
            provider_sources[target_idx] = new_source_config

        # 鏇存柊寮曠敤浜嗚 provider_source 鐨?providers
        affected_providers = []
        for provider in self.config.get("provider", []):
            if provider.get("provider_source_id") == old_id:
                provider["provider_source_id"] = new_source_config["id"]
                affected_providers.append(provider)

        # 鍐欏洖閰嶇疆
        self.config["provider_sources"] = provider_sources

        try:
            save_config(self.config, self.config, is_core=True)
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(str(e)).__dict__

        # 閲嶈浇鍙楀奖鍝嶇殑 providers锛屼娇鏂扮殑 source 閰嶇疆鐢熸晥
        reload_errors = []
        prov_mgr = self.core_lifecycle.provider_manager
        for provider in affected_providers:
            try:
                await prov_mgr.reload(provider)
            except Exception as e:
                logger.error(traceback.format_exc())
                reload_errors.append(f"{provider.get('id')}: {e}")

        if reload_errors:
            return (
                Response()
                .error("鏇存柊鎴愬姛锛屼絾閮ㄥ垎鎻愪緵鍟嗛噸杞藉け璐? " + ", ".join(reload_errors))
                .__dict__
            )

        return Response().ok(message="鏇存柊 provider source 鎴愬姛").__dict__

    async def get_provider_template(self):
        config_schema, provider_i18n_translations, provider_type_metadata = (
            self._build_provider_schema_with_i18n()
        )
        data = {
            "config_schema": config_schema,
            "providers": astrbot_config["provider"],
            "provider_sources": astrbot_config["provider_sources"],
            "provider_i18n_translations": provider_i18n_translations,
            "provider_type_metadata": provider_type_metadata,
        }
        return Response().ok(data=data).__dict__

    async def get_uc_table(self):
        """鑾峰彇 UMOP 閰嶇疆璺敱琛?""
        return Response().ok({"routing": self.ucr.umop_to_conf_id}).__dict__

    async def update_ucr_all(self):
        """鏇存柊 UMOP 閰嶇疆璺敱琛ㄧ殑鍏ㄩ儴鍐呭"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        new_routing = post_data.get("routing", None)

        if not new_routing or not isinstance(new_routing, dict):
            return Response().error("缂哄皯鎴栭敊璇殑璺敱琛ㄦ暟鎹?).__dict__

        try:
            await self.ucr.update_routing_data(new_routing)
            return Response().ok(message="鏇存柊鎴愬姛").__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鏇存柊璺敱琛ㄥけ璐? {e!s}").__dict__

    async def update_ucr(self):
        """鏇存柊 UMOP 閰嶇疆璺敱琛?""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        umo = post_data.get("umo", None)
        conf_id = post_data.get("conf_id", None)

        if not umo or not conf_id:
            return Response().error("缂哄皯 UMO 鎴栭厤缃枃浠?ID").__dict__

        try:
            await self.ucr.update_route(umo, conf_id)
            return Response().ok(message="鏇存柊鎴愬姛").__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鏇存柊璺敱琛ㄥけ璐? {e!s}").__dict__

    async def delete_ucr(self):
        """鍒犻櫎 UMOP 閰嶇疆璺敱琛ㄤ腑鐨勪竴椤?""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        umo = post_data.get("umo", None)

        if not umo:
            return Response().error("缂哄皯 UMO").__dict__

        try:
            if umo in self.ucr.umop_to_conf_id:
                del self.ucr.umop_to_conf_id[umo]
                await self.ucr.update_routing_data(self.ucr.umop_to_conf_id)
            return Response().ok(message="鍒犻櫎鎴愬姛").__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鍒犻櫎璺敱琛ㄩ」澶辫触: {e!s}").__dict__

    async def get_default_config(self):
        """鑾峰彇榛樿閰嶇疆鏂囦欢"""
        metadata = ConfigMetadataI18n.convert_to_i18n_keys(CONFIG_METADATA_3)
        return Response().ok({"config": DEFAULT_CONFIG, "metadata": metadata}).__dict__

    async def get_abconf_list(self):
        """鑾峰彇鎵€鏈?AstrBot 閰嶇疆鏂囦欢鐨勫垪琛?""
        abconf_list = self.acm.get_conf_list()
        return Response().ok({"info_list": abconf_list}).__dict__

    async def create_abconf(self):
        """鍒涘缓鏂扮殑 AstrBot 閰嶇疆鏂囦欢"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__
        name = post_data.get("name", None)
        config = post_data.get("config", DEFAULT_CONFIG)

        try:
            conf_id = self.acm.create_conf(name=name, config=config)
            await self.core_lifecycle.reload_pipeline_scheduler(conf_id)
            return Response().ok(message="鍒涘缓鎴愬姛", data={"conf_id": conf_id}).__dict__
        except ValueError as e:
            return Response().error(str(e)).__dict__

    async def get_abconf(self):
        """鑾峰彇鎸囧畾 AstrBot 閰嶇疆鏂囦欢"""
        abconf_id = request.args.get("id")
        system_config = request.args.get("system_config", "0").lower() == "1"
        if not abconf_id and not system_config:
            return Response().error("缂哄皯閰嶇疆鏂囦欢 ID").__dict__

        try:
            if system_config:
                abconf = self.acm.confs["default"]
                metadata = ConfigMetadataI18n.convert_to_i18n_keys(
                    CONFIG_METADATA_3_SYSTEM
                )
                return Response().ok({"config": abconf, "metadata": metadata}).__dict__
            if abconf_id is None:
                raise ValueError("abconf_id cannot be None")
            abconf = self.acm.confs[abconf_id]
            metadata = ConfigMetadataI18n.convert_to_i18n_keys(CONFIG_METADATA_3)
            return Response().ok({"config": abconf, "metadata": metadata}).__dict__
        except ValueError as e:
            return Response().error(str(e)).__dict__

    async def delete_abconf(self):
        """鍒犻櫎鎸囧畾 AstrBot 閰嶇疆鏂囦欢"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        conf_id = post_data.get("id")
        if not conf_id:
            return Response().error("缂哄皯閰嶇疆鏂囦欢 ID").__dict__

        try:
            success = self.acm.delete_conf(conf_id)
            if success:
                self.core_lifecycle.pipeline_scheduler_mapping.pop(conf_id, None)
                return Response().ok(message="鍒犻櫎鎴愬姛").__dict__
            return Response().error("鍒犻櫎澶辫触").__dict__
        except ValueError as e:
            return Response().error(str(e)).__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鍒犻櫎閰嶇疆鏂囦欢澶辫触: {e!s}").__dict__

    async def update_abconf(self):
        """鏇存柊鎸囧畾 AstrBot 閰嶇疆鏂囦欢淇℃伅"""
        post_data = await request.json
        if not post_data:
            return Response().error("缂哄皯閰嶇疆鏁版嵁").__dict__

        conf_id = post_data.get("id")
        if not conf_id:
            return Response().error("缂哄皯閰嶇疆鏂囦欢 ID").__dict__

        name = post_data.get("name")

        try:
            success = self.acm.update_conf_info(conf_id, name=name)
            if success:
                return Response().ok(message="鏇存柊鎴愬姛").__dict__
            return Response().error("鏇存柊澶辫触").__dict__
        except ValueError as e:
            return Response().error(str(e)).__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鏇存柊閰嶇疆鏂囦欢澶辫触: {e!s}").__dict__

    async def _test_single_provider(self, provider):
        """杈呭姪鍑芥暟锛氭祴璇曞崟涓?provider 鐨勫彲鐢ㄦ€?""
        meta = provider.meta()
        provider_name = provider.provider_config.get("id", "Unknown Provider")
        provider_capability_type = meta.provider_type

        status_info = {
            "id": getattr(meta, "id", "Unknown ID"),
            "model": getattr(meta, "model", "Unknown Model"),
            "type": provider_capability_type.value,
            "name": provider_name,
            "status": "unavailable",  # 榛樿涓轰笉鍙敤
            "error": None,
        }
        logger.debug(
            f"Attempting to check provider: {status_info['name']} (ID: {status_info['id']}, Type: {status_info['type']}, Model: {status_info['model']})",
        )

        try:
            await provider.test()
            status_info["status"] = "available"
            logger.info(
                f"Provider {status_info['name']} (ID: {status_info['id']}) is available.",
            )
        except Exception as e:
            error_message = str(e)
            status_info["error"] = error_message
            logger.warning(
                f"Provider {status_info['name']} (ID: {status_info['id']}) is unavailable. Error: {error_message}",
            )
            logger.debug(
                f"Traceback for {status_info['name']}:\n{traceback.format_exc()}",
            )

        return status_info

    def _error_response(
        self,
        message: str,
        status_code: int = 500,
        log_fn=logger.error,
    ):
        log_fn(message)
        # 璁板綍鏇磋缁嗙殑traceback淇℃伅锛屼絾鍙湪鏄弗閲嶉敊璇椂
        if status_code == 500:
            log_fn(traceback.format_exc())
        return Response().error(message).__dict__

    async def check_one_provider_status(self):
        """API: check a single LLM Provider's status by id"""
        provider_id = request.args.get("id")
        if not provider_id:
            return self._error_response(
                "Missing provider_id parameter",
                400,
                logger.warning,
            )

        logger.info(f"API call: /config/provider/check_one id={provider_id}")
        try:
            prov_mgr = self.core_lifecycle.provider_manager
            target = prov_mgr.inst_map.get(provider_id)

            if not target:
                logger.warning(
                    f"Provider with id '{provider_id}' not found in provider_manager.",
                )
                return (
                    Response()
                    .error(f"Provider with id '{provider_id}' not found")
                    .__dict__
                )

            result = await self._test_single_provider(target)
            return Response().ok(result).__dict__

        except Exception as e:
            return self._error_response(
                f"Critical error checking provider {provider_id}: {e}",
                500,
            )

    async def get_configs(self):
        logger.warning(
            "[config-route] /api/config/get called plugin_name=%s",
            request.args.get("plugin_name", None),
        )
        # plugin_name 涓虹┖鏃惰繑鍥?AstrBot 閰嶇疆
        # 鍚﹀垯杩斿洖鎸囧畾 plugin_name 鐨勬彃浠堕厤缃?
        plugin_name = request.args.get("plugin_name", None)
        if not plugin_name:
            return Response().ok(await self._get_astrbot_config()).__dict__
        return Response().ok(await self._get_plugin_config(plugin_name)).__dict__

    async def get_provider_config_list(self):
        provider_type = request.args.get("provider_type", None)
        if not provider_type:
            return Response().error("缂哄皯鍙傛暟 provider_type").__dict__
        provider_type_ls = provider_type.split(",")
        provider_list = []
        ps = self.core_lifecycle.provider_manager.providers_config
        p_source_pt = {
            psrc["id"]: psrc.get("provider_type", "chat_completion")
            for psrc in self.core_lifecycle.provider_manager.provider_sources_config
        }
        for provider in ps:
            ps_id = provider.get("provider_source_id", None)
            if (
                ps_id
                and ps_id in p_source_pt
                and p_source_pt[ps_id] in provider_type_ls
            ):
                # chat
                prov = self.core_lifecycle.provider_manager.get_merged_provider_config(
                    provider
                )
                provider_list.append(prov)
            elif not ps_id and provider.get("provider_type", "") in provider_type_ls:
                # agent runner, embedding, etc
                provider_list.append(provider)
        return Response().ok(provider_list).__dict__

    async def get_provider_model_list(self):
        """鑾峰彇鎸囧畾鎻愪緵鍟嗙殑妯″瀷鍒楄〃"""
        provider_id = request.args.get("provider_id", None)
        if not provider_id:
            return Response().error("缂哄皯鍙傛暟 provider_id").__dict__

        prov_mgr = self.core_lifecycle.provider_manager
        provider = prov_mgr.inst_map.get(provider_id, None)
        if not provider:
            return Response().error(f"鏈壘鍒?ID 涓?{provider_id} 鐨勬彁渚涘晢").__dict__
        if not isinstance(provider, Provider):
            return (
                Response()
                .error(f"鎻愪緵鍟?{provider_id} 绫诲瀷涓嶆敮鎸佽幏鍙栨ā鍨嬪垪琛?)
                .__dict__
            )

        try:
            models = await provider.get_models()
            models = models or []

            metadata_map = {}
            for model_id in models:
                meta = LLM_METADATAS.get(model_id)
                if meta:
                    metadata_map[model_id] = meta

            ret = {
                "models": models,
                "provider_id": provider_id,
                "model_metadata": metadata_map,
            }
            return Response().ok(ret).__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(str(e)).__dict__

    async def get_embedding_dim(self):
        """鑾峰彇宓屽叆妯″瀷鐨勭淮搴?""
        post_data = await request.json
        provider_config = post_data.get("provider_config", None)
        if not provider_config:
            return Response().error("缂哄皯鍙傛暟 provider_config").__dict__

        try:
            # 鍔ㄦ€佸鍏?EmbeddingProvider
            from astrbot.core.provider.provider import EmbeddingProvider
            from astrbot.core.provider.register import provider_cls_map

            # 鑾峰彇 provider 绫诲瀷
            provider_type = provider_config.get("type", None)
            if not provider_type:
                return Response().error("provider_config 缂哄皯 type 瀛楁").__dict__

            # 棣栨娣诲姞鏌愮被鎻愪緵鍟嗘椂锛宲rovider_cls_map 鍙兘灏氭湭娉ㄥ唽璇ラ€傞厤鍣?
            if provider_type not in provider_cls_map:
                try:
                    self.core_lifecycle.provider_manager.dynamic_import_provider(
                        provider_type,
                    )
                except ImportError:
                    logger.error(traceback.format_exc())
                    return (
                        Response()
                        .error(
                            "鎻愪緵鍟嗛€傞厤鍣ㄥ姞杞藉け璐ワ紝璇锋鏌ユ彁渚涘晢绫诲瀷閰嶇疆鎴栨煡鐪嬫湇鍔＄鏃ュ織"
                        )
                        .__dict__
                    )

            # 鑾峰彇瀵瑰簲鐨?provider 绫?
            if provider_type not in provider_cls_map:
                return (
                    Response()
                    .error(f"鏈壘鍒伴€傜敤浜?{provider_type} 鐨勬彁渚涘晢閫傞厤鍣?)
                    .__dict__
                )

            provider_metadata = provider_cls_map[provider_type]
            cls_type = provider_metadata.cls_type

            if not cls_type:
                return Response().error(f"鏃犳硶鎵惧埌 {provider_type} 鐨勭被").__dict__

            # 瀹炰緥鍖?provider
            inst = cls_type(provider_config, {})

            # 妫€鏌ユ槸鍚︽槸 EmbeddingProvider
            if not isinstance(inst, EmbeddingProvider):
                return Response().error("鎻愪緵鍟嗕笉鏄?EmbeddingProvider 绫诲瀷").__dict__

            init_fn = getattr(inst, "initialize", None)
            if inspect.iscoroutinefunction(init_fn):
                await init_fn()

            # 閫氳繃瀹為檯璇锋眰楠岃瘉褰撳墠 embedding_dimensions 鏄惁鍙敤
            vec = await inst.get_embedding("echo")
            dim = len(vec)

            logger.info(
                f"妫€娴嬪埌 {provider_config.get('id', 'unknown')} 鐨勫祵鍏ュ悜閲忕淮搴︿负 {dim}",
            )

            return Response().ok({"embedding_dimensions": dim}).__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鑾峰彇宓屽叆缁村害澶辫触: {e!s}").__dict__

    async def get_provider_source_models(self):
        """鑾峰彇鎸囧畾 provider_source 鏀寔鐨勬ā鍨嬪垪琛?

        鏈川涓婁細涓存椂鍒濆鍖栦竴涓?Provider 瀹炰緥锛岃皟鐢?get_models() 鑾峰彇妯″瀷鍒楄〃锛岀劧鍚庨攢姣佸疄渚?
        """
        provider_source_id = request.args.get("source_id")
        if not provider_source_id:
            return Response().error("缂哄皯鍙傛暟 source_id").__dict__

        try:
            from astrbot.core.provider.register import provider_cls_map

            # 浠庨厤缃腑鏌ユ壘瀵瑰簲鐨?provider_source
            provider_sources = self.config.get("provider_sources", [])
            provider_source = None
            for ps in provider_sources:
                if ps.get("id") == provider_source_id:
                    provider_source = ps
                    break

            if not provider_source:
                return (
                    Response()
                    .error(f"鏈壘鍒?ID 涓?{provider_source_id} 鐨?provider_source")
                    .__dict__
                )

            # 鑾峰彇 provider 绫诲瀷
            provider_type = provider_source.get("type", None)
            if not provider_type:
                return Response().error("provider_source 缂哄皯 type 瀛楁").__dict__

            try:
                self.core_lifecycle.provider_manager.dynamic_import_provider(
                    provider_type
                )
            except ImportError as e:
                logger.error(traceback.format_exc())
                return Response().error(f"鍔ㄦ€佸鍏ユ彁渚涘晢閫傞厤鍣ㄥけ璐? {e!s}").__dict__

            # 鑾峰彇瀵瑰簲鐨?provider 绫?
            if provider_type not in provider_cls_map:
                return (
                    Response()
                    .error(f"鏈壘鍒伴€傜敤浜?{provider_type} 鐨勬彁渚涘晢閫傞厤鍣?)
                    .__dict__
                )

            provider_metadata = provider_cls_map[provider_type]
            cls_type = provider_metadata.cls_type

            if not cls_type:
                return Response().error(f"鏃犳硶鎵惧埌 {provider_type} 鐨勭被").__dict__

            # 妫€鏌ユ槸鍚︽槸 Provider 绫诲瀷
            if not issubclass(cls_type, Provider):
                return (
                    Response()
                    .error(f"鎻愪緵鍟?{provider_type} 涓嶆敮鎸佽幏鍙栨ā鍨嬪垪琛?)
                    .__dict__
                )

            # 涓存椂瀹炰緥鍖?provider
            inst = cls_type(provider_source, {})

            # 濡傛灉鏈?initialize 鏂规硶锛岃皟鐢ㄥ畠
            init_fn = getattr(inst, "initialize", None)
            if inspect.iscoroutinefunction(init_fn):
                await init_fn()

            # 鑾峰彇妯″瀷鍒楄〃
            models = await inst.get_models()
            models = models or []

            metadata_map = {}
            for model_id in models:
                meta = LLM_METADATAS.get(model_id)
                if meta:
                    metadata_map[model_id] = meta

            # 閿€姣佸疄渚嬶紙濡傛灉鏈?terminate 鏂规硶锛?
            terminate_fn = getattr(inst, "terminate", None)
            if inspect.iscoroutinefunction(terminate_fn):
                await terminate_fn()

            logger.info(
                f"鑾峰彇鍒?provider_source {provider_source_id} 鐨勬ā鍨嬪垪琛? {models}",
            )

            return (
                Response()
                .ok({"models": models, "model_metadata": metadata_map})
                .__dict__
            )
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(f"鑾峰彇妯″瀷鍒楄〃澶辫触: {e!s}").__dict__

    async def get_platform_list(self):
        """鑾峰彇鎵€鏈夊钩鍙扮殑鍒楄〃"""
        platform_list = []
        for platform in self.config["platform"]:
            platform_list.append(platform)
        return Response().ok({"platforms": platform_list}).__dict__

    async def post_astrbot_configs(self):
        data = await request.json
        config = data.get("config", None)
        conf_id = data.get("conf_id", None)

        try:
            # 涓嶆洿鏂?provider_sources, provider, platform
            # 杩欎簺閰嶇疆鏈夊崟鐙殑鎺ュ彛杩涜鏇存柊
            if conf_id == "default":
                no_update_keys = ["provider_sources", "provider", "platform"]
                for key in no_update_keys:
                    config[key] = self.acm.default_conf[key]

            await self._save_astrbot_configs(config, conf_id)
            await self.core_lifecycle.reload_pipeline_scheduler(conf_id)

            # Non-blocking Bay connectivity check
            warning = await _validate_neo_connectivity(config)
            if warning:
                return Response().ok(None, f"淇濆瓨鎴愬姛銆倇warning}").__dict__
            return Response().ok(None, "淇濆瓨鎴愬姛~").__dict__
        except Exception as e:
            logger.error(traceback.format_exc())
            return Response().error(str(e)).__dict__

    async def post_plugin_configs(self):
        post_configs = await request.json
        plugin_name = request.args.get("plugin_name", "unknown")
        try:
            await self._save_plugin_configs(post_configs, plugin_name)
            await self.core_lifecycle.plugin_manager.reload(plugin_name)
            return (
                Response()
                .ok(None, f"淇濆瓨鎻掍欢 {plugin_name} 鎴愬姛~ 鏈哄櫒浜烘鍦ㄧ儹閲嶈浇鎻掍欢銆?)
                .__dict__
            )
        except Exception as e:
            return Response().error(str(e)).__dict__

    def _get_plugin_metadata_by_name(self, plugin_name: str) -> StarMetadata | None:
        for plugin_md in star_registry:
            if plugin_md.name == plugin_name:
                return plugin_md
        return None

    def _resolve_config_file_scope(
        self,
    ) -> tuple[str, str, str, StarMetadata, AstrBotConfig]:
        """灏嗚姹傚弬鏁拌В鏋愪负涓€涓槑纭殑閰嶇疆浣滅敤鍩熴€?

        褰撳墠鏀寔鐨?scope锛?
        - scope=plugin锛歯ame=<plugin_name>锛宬ey=<config_key_path>
        """

        scope = request.args.get("scope") or "plugin"
        name = request.args.get("name")
        key_path = request.args.get("key")

        if scope != "plugin":
            raise ValueError(f"Unsupported scope: {scope}")
        if not name or not key_path:
            raise ValueError("Missing name or key parameter")

        md = self._get_plugin_metadata_by_name(name)
        if not md or not md.config:
            raise ValueError(f"Plugin {name} not found or has no config")

        return scope, name, key_path, md, md.config

    async def upload_config_file(self):
        """涓婁紶鏂囦欢鍒版彃浠舵暟鎹洰褰曪紙鐢ㄤ簬鏌愪釜 file 绫诲瀷閰嶇疆椤癸級銆?""

        try:
            scope, name, key_path, md, config = self._resolve_config_file_scope()
        except ValueError as e:
            return Response().error(str(e)).__dict__

        meta = get_schema_item(getattr(config, "schema", None), key_path)
        if not meta or meta.get("type") != "file":
            return Response().error("Config item not found or not file type").__dict__

        file_types = meta.get("file_types")
        allowed_exts: list[str] = []
        if isinstance(file_types, list):
            allowed_exts = [
                str(ext).lstrip(".").lower() for ext in file_types if str(ext).strip()
            ]

        files = await request.files
        if not files:
            return Response().error("No files uploaded").__dict__

        storage_root_path = Path(get_astrbot_plugin_data_path()).resolve(strict=False)
        plugin_root_path = (storage_root_path / name).resolve(strict=False)
        try:
            plugin_root_path.relative_to(storage_root_path)
        except ValueError:
            return Response().error("Invalid name parameter").__dict__
        plugin_root_path.mkdir(parents=True, exist_ok=True)

        uploaded: list[str] = []
        folder = config_key_to_folder(key_path)
        errors: list[str] = []
        for file in files.values():
            filename = sanitize_filename(file.filename or "")
            if not filename:
                errors.append("Invalid filename")
                continue

            file_size = getattr(file, "content_length", None)
            if isinstance(file_size, int) and file_size > MAX_FILE_BYTES:
                errors.append(f"File too large: {filename}")
                continue

            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            if allowed_exts and ext not in allowed_exts:
                errors.append(f"Unsupported file type: {filename}")
                continue

            rel_path = f"files/{folder}/{filename}"
            save_path = (plugin_root_path / rel_path).resolve(strict=False)
            try:
                save_path.relative_to(plugin_root_path)
            except ValueError:
                errors.append(f"Invalid path: {filename}")
                continue

            save_path.parent.mkdir(parents=True, exist_ok=True)
            await file.save(str(save_path))
            if save_path.is_file() and save_path.stat().st_size > MAX_FILE_BYTES:
                save_path.unlink()
                errors.append(f"File too large: {filename}")
                continue
            uploaded.append(rel_path)

        if not uploaded:
            return (
                Response()
                .error(
                    "Upload failed: " + ", ".join(errors)
                    if errors
                    else "Upload failed",
                )
                .__dict__
            )

        return Response().ok({"uploaded": uploaded, "errors": errors}).__dict__

    async def delete_config_file(self):
        """鍒犻櫎鎻掍欢鏁版嵁鐩綍涓殑鏂囦欢銆?""

        scope = request.args.get("scope") or "plugin"
        name = request.args.get("name")
        if not name:
            return Response().error("Missing name parameter").__dict__
        if scope != "plugin":
            return Response().error(f"Unsupported scope: {scope}").__dict__

        data = await request.get_json()
        rel_path = data.get("path") if isinstance(data, dict) else None
        rel_path = normalize_rel_path(rel_path)
        if not rel_path or not rel_path.startswith("files/"):
            return Response().error("Invalid path parameter").__dict__

        md = self._get_plugin_metadata_by_name(name)
        if not md:
            return Response().error(f"Plugin {name} not found").__dict__

        storage_root_path = Path(get_astrbot_plugin_data_path()).resolve(strict=False)
        plugin_root_path = (storage_root_path / name).resolve(strict=False)
        try:
            plugin_root_path.relative_to(storage_root_path)
        except ValueError:
            return Response().error("Invalid name parameter").__dict__
        target_path = (plugin_root_path / rel_path).resolve(strict=False)
        try:
            target_path.relative_to(plugin_root_path)
        except ValueError:
            return Response().error("Invalid path parameter").__dict__
        if target_path.is_file():
            target_path.unlink()

        return Response().ok(None, "Deleted").__dict__

    async def get_config_file_list(self):
        """鑾峰彇閰嶇疆椤瑰搴旂洰褰曚笅鐨勬枃浠跺垪琛ㄣ€?""

        try:
            _, name, key_path, _, config = self._resolve_config_file_scope()
        except ValueError as e:
            return Response().error(str(e)).__dict__

        meta = get_schema_item(getattr(config, "schema", None), key_path)
        if not meta or meta.get("type") != "file":
            return Response().error("Config item not found or not file type").__dict__

        storage_root_path = Path(get_astrbot_plugin_data_path()).resolve(strict=False)
        plugin_root_path = (storage_root_path / name).resolve(strict=False)
        try:
            plugin_root_path.relative_to(storage_root_path)
        except ValueError:
            return Response().error("Invalid name parameter").__dict__

        folder = config_key_to_folder(key_path)
        target_dir = (plugin_root_path / "files" / folder).resolve(strict=False)
        try:
            target_dir.relative_to(plugin_root_path)
        except ValueError:
            return Response().error("Invalid path parameter").__dict__

        if not target_dir.exists() or not target_dir.is_dir():
            return Response().ok({"files": []}).__dict__

        files: list[str] = []
        for path in target_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel_path = path.relative_to(plugin_root_path).as_posix()
            except ValueError:
                continue
            if rel_path.startswith("files/"):
                files.append(rel_path)

        return Response().ok({"files": files}).__dict__

    async def post_new_platform(self):
        new_platform_config = await request.json

        # 濡傛灉鏄敮鎸佺粺涓€ webhook 妯″紡鐨勫钩鍙帮紝鐢熸垚 webhook_uuid
        ensure_platform_webhook_config(new_platform_config)

        self.config["platform"].append(new_platform_config)
        try:
            save_config(self.config, self.config, is_core=True)
            await self.core_lifecycle.platform_manager.load_platform(
                new_platform_config,
            )
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鏂板骞冲彴閰嶇疆鎴愬姛~").__dict__

    async def post_new_provider(self):
        new_provider_config = await request.json

        try:
            await self.core_lifecycle.provider_manager.create_provider(
                new_provider_config
            )
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鏂板鏈嶅姟鎻愪緵鍟嗛厤缃垚鍔?).__dict__

    async def post_update_platform(self):
        update_platform_config = await request.json
        origin_platform_id = update_platform_config.get("id", None)
        new_config = update_platform_config.get("config", None)
        if not origin_platform_id or not new_config:
            return Response().error("鍙傛暟閿欒").__dict__

        if origin_platform_id != new_config.get("id", None):
            return Response().error("鏈哄櫒浜哄悕绉颁笉鍏佽淇敼").__dict__

        # 濡傛灉鏄敮鎸佺粺涓€ webhook 妯″紡鐨勫钩鍙帮紝涓斿惎鐢ㄤ簡缁熶竴 webhook 妯″紡锛岀‘淇濇湁 webhook_uuid
        ensure_platform_webhook_config(new_config)

        for i, platform in enumerate(self.config["platform"]):
            if platform["id"] == origin_platform_id:
                self.config["platform"][i] = new_config
                break
        else:
            return Response().error("鏈壘鍒板搴斿钩鍙?).__dict__

        try:
            save_config(self.config, self.config, is_core=True)
            await self.core_lifecycle.platform_manager.reload(new_config)
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鏇存柊骞冲彴閰嶇疆鎴愬姛~").__dict__

    async def post_update_provider(self):
        update_provider_config = await request.json
        origin_provider_id = update_provider_config.get("id", None)
        new_config = update_provider_config.get("config", None)
        if not origin_provider_id or not new_config:
            return Response().error("鍙傛暟閿欒").__dict__

        try:
            await self.core_lifecycle.provider_manager.update_provider(
                origin_provider_id, new_config
            )
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鏇存柊鎴愬姛锛屽凡缁忓疄鏃剁敓鏁垀").__dict__

    async def post_delete_platform(self):
        platform_id = await request.json
        platform_id = platform_id.get("id")
        for i, platform in enumerate(self.config["platform"]):
            if platform["id"] == platform_id:
                del self.config["platform"][i]
                break
        else:
            return Response().error("鏈壘鍒板搴斿钩鍙?).__dict__
        try:
            save_config(self.config, self.config, is_core=True)
            await self.core_lifecycle.platform_manager.terminate_platform(platform_id)
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鍒犻櫎骞冲彴閰嶇疆鎴愬姛~").__dict__

    async def post_delete_provider(self):
        provider_id = await request.json
        provider_id = provider_id.get("id", "")
        if not provider_id:
            return Response().error("缂哄皯鍙傛暟 id").__dict__

        try:
            await self.core_lifecycle.provider_manager.delete_provider(
                provider_id=provider_id
            )
        except Exception as e:
            return Response().error(str(e)).__dict__
        return Response().ok(None, "鍒犻櫎鎴愬姛锛屽凡缁忓疄鏃剁敓鏁堛€?).__dict__

    async def get_llm_tools(self):
        """鑾峰彇鍑芥暟璋冪敤宸ュ叿銆傚寘鍚簡鏈湴鍔犺浇鐨勪互鍙?MCP 鏈嶅姟鐨勫伐鍏?""
        tool_mgr = self.core_lifecycle.provider_manager.llm_tools
        tools = tool_mgr.get_func_desc_openai_style()
        return Response().ok(tools).__dict__

    async def _register_platform_logo(self, platform, platform_default_tmpl) -> None:
        """娉ㄥ唽骞冲彴logo鏂囦欢骞剁敓鎴愯闂护鐗?""
        if not platform.logo_path:
            return

        try:
            # 妫€鏌ョ紦瀛?
            cache_key = f"{platform.name}:{platform.logo_path}"
            if cache_key in self._logo_token_cache:
                cached_token = self._logo_token_cache[cache_key]
                # 纭繚platform_default_tmpl[platform.name]瀛樺湪涓斾负瀛楀吀
                if platform.name not in platform_default_tmpl or not isinstance(
                    platform_default_tmpl[platform.name], dict
                ):
                    platform_default_tmpl[platform.name] = {}
                platform_default_tmpl[platform.name]["logo_token"] = cached_token
                logger.debug(f"Using cached logo token for platform {platform.name}")
                return

            # 鑾峰彇骞冲彴閫傞厤鍣ㄧ被
            platform_cls = platform_cls_map.get(platform.name)
            if not platform_cls:
                logger.warning(f"Platform class not found for {platform.name}")
                return

            # 鑾峰彇鎻掍欢鐩綍璺緞
            module_file = inspect.getfile(platform_cls)
            plugin_dir = os.path.dirname(module_file)

            # 瑙ｆ瀽logo鏂囦欢璺緞
            logo_file_path = os.path.join(plugin_dir, platform.logo_path)

            # 妫€鏌ユ枃浠舵槸鍚﹀瓨鍦ㄥ苟娉ㄥ唽浠ょ墝
            if os.path.exists(logo_file_path):
                logo_token = await file_token_service.register_file(
                    logo_file_path,
                    timeout=3600,
                )

                # 纭繚platform_default_tmpl[platform.name]瀛樺湪涓斾负瀛楀吀
                if platform.name not in platform_default_tmpl or not isinstance(
                    platform_default_tmpl[platform.name], dict
                ):
                    platform_default_tmpl[platform.name] = {}

                platform_default_tmpl[platform.name]["logo_token"] = logo_token

                # 缂撳瓨token
                self._logo_token_cache[cache_key] = logo_token

                logger.debug(f"Logo token registered for platform {platform.name}")
            else:
                logger.warning(
                    f"Platform {platform.name} logo file not found: {logo_file_path}",
                )

        except (ImportError, AttributeError) as e:
            logger.warning(
                f"Failed to import required modules for platform {platform.name}: {e}",
            )
        except OSError as e:
            logger.warning(f"File system error for platform {platform.name} logo: {e}")
        except Exception as e:
            logger.warning(
                f"Unexpected error registering logo for platform {platform.name}: {e}",
            )

    def _inject_platform_metadata_with_i18n(
        self, platform, platform_items_to_inject: dict, platform_i18n_translations: dict
    ):
        """灏嗛厤缃厓鏁版嵁娉ㄥ叆鍒?metadata 涓苟澶勭悊鍥介檯鍖栭敭杞崲銆?""
        if platform.i18n_resources:
            i18n_prefix = f"platform_group.platform.{platform.name}"

            for lang, lang_data in platform.i18n_resources.items():
                platform_i18n_translations.setdefault(lang, {}).setdefault(
                    "platform_group", {}
                ).setdefault("platform", {})[platform.name] = lang_data

            for field_key, field_value in platform_items_to_inject.items():
                for key in ("description", "hint", "labels"):
                    if key in field_value:
                        field_value[key] = f"{i18n_prefix}.{field_key}.{key}"

    def _apply_dynamic_i18n_keys(self, items: dict, i18n_prefix: str):
        """Replace configurable text fields with dynamic i18n keys recursively."""
        for field_key, field_value in items.items():
            if not isinstance(field_value, dict):
                continue

            field_prefix = f"{i18n_prefix}.{field_key}"
            for key in ("description", "hint", "labels", "name"):
                if key in field_value:
                    field_value[key] = f"{field_prefix}.{key}"

            if isinstance(field_value.get("items"), dict):
                self._apply_dynamic_i18n_keys(field_value["items"], field_prefix)

            if isinstance(field_value.get("template_schema"), dict):
                self._apply_dynamic_i18n_keys(
                    field_value["template_schema"],
                    f"{field_prefix}.template_schema",
                )

    def _collect_provider_i18n_translations(
        self, provider, provider_i18n_translations: dict
    ):
        """Collect runtime i18n resources for a provider adapter."""
        if not provider.i18n_resources:
            return

        for lang, lang_data in provider.i18n_resources.items():
            provider_i18n_translations.setdefault(lang, {}).setdefault(
                "provider_group", {}
            ).setdefault("provider", {})[provider.type] = copy.deepcopy(lang_data)

    def _build_provider_schema_with_i18n(self) -> tuple[dict, dict, dict]:
        provider_metadata = ConfigMetadataI18n.convert_to_i18n_keys(
            {
                "provider_group": {
                    "metadata": {
                        "provider": copy.deepcopy(
                            CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]
                        )
                    }
                }
            }
        )
        provider_i18n_translations = {}
        provider_type_metadata = {}

        provider_default_tmpl = provider_metadata["provider_group"]["metadata"][
            "provider"
        ]["config_template"]
        for provider in provider_registry:
            self._collect_provider_i18n_translations(
                provider, provider_i18n_translations
            )

            if provider.default_config_tmpl:
                provider_default_tmpl[provider.type] = copy.deepcopy(
                    provider.default_config_tmpl
                )
                display_name = provider.provider_display_name or provider.type
                if provider.i18n_resources:
                    display_name = f"provider_group.provider.{provider.type}.name"
                provider_default_tmpl[provider.type]["_display_name"] = display_name

            if provider.config_metadata:
                provider_items_to_inject = copy.deepcopy(provider.config_metadata)
                if provider.i18n_resources:
                    self._apply_dynamic_i18n_keys(
                        provider_items_to_inject,
                        f"provider_group.provider.{provider.type}",
                    )
                provider_type_metadata[provider.type] = {
                    "items": provider_items_to_inject
                }

        return (
            {"provider": provider_metadata["provider_group"]["metadata"]["provider"]},
            provider_i18n_translations,
            provider_type_metadata,
        )

    async def _build_platform_schema_with_i18n(self) -> tuple[dict, dict, dict]:
        metadata = copy.deepcopy(CONFIG_METADATA_2)
        platform_i18n = ConfigMetadataI18n.convert_to_i18n_keys(
            {
                "platform_group": {
                    "metadata": {
                        "platform": copy.deepcopy(
                            CONFIG_METADATA_2["platform_group"]["metadata"]["platform"]
                        )
                    }
                }
            }
        )
        metadata["platform_group"]["metadata"]["platform"] = platform_i18n[
            "platform_group"
        ]["metadata"]["platform"]

        platform_default_tmpl = metadata["platform_group"]["metadata"]["platform"][
            "config_template"
        ]
        platform_i18n_translations = {}
        platform_type_metadata = {}
        logo_registration_tasks = []

        for platform in platform_registry:
            if platform.default_config_tmpl:
                platform_default_tmpl[platform.name] = copy.deepcopy(
                    platform.default_config_tmpl
                )

            if platform.config_metadata:
                platform_items_to_inject = copy.deepcopy(platform.config_metadata)
                self._inject_platform_metadata_with_i18n(
                    platform,
                    platform_items_to_inject,
                    platform_i18n_translations,
                )
                platform_type_metadata[platform.name] = {"items": platform_items_to_inject}

            if platform.logo_path and platform.default_config_tmpl:
                logo_registration_tasks.append(
                    self._register_platform_logo(platform, platform_default_tmpl),
                )

            logger.info(
                "[platform-schema] name=%s template_keys=%s metadata_keys=%s i18n_locales=%s",
                platform.name,
                list((platform.default_config_tmpl or {}).keys()),
                list((platform.config_metadata or {}).keys()),
                list((platform.i18n_resources or {}).keys()),
            )

        if logo_registration_tasks:
            await asyncio.gather(*logo_registration_tasks, return_exceptions=True)

        logger.info(
            "[platform-schema] templates=%s type_metadata=%s translations=%s",
            list(platform_default_tmpl.keys()),
            list(platform_type_metadata.keys()),
            list(platform_i18n_translations.keys()),
        )

        return metadata, platform_i18n_translations, platform_type_metadata

    async def _get_astrbot_config(self):
        logger.warning("[config-route] _get_astrbot_config start")
        config = self.config
        (
            metadata,
            platform_i18n_translations,
            platform_type_metadata,
        ) = await self._build_platform_schema_with_i18n()

        # 鏈嶅姟鎻愪緵鍟嗙殑榛樿閰嶇疆妯℃澘娉ㄥ叆
        provider_schema, provider_i18n_translations, provider_type_metadata = (
            self._build_provider_schema_with_i18n()
        )
        metadata["provider_group"]["metadata"]["provider"] = provider_schema["provider"]

        return {
            "metadata": metadata,
            "config": config,
            "platform_i18n_translations": platform_i18n_translations,
            "platform_type_metadata": platform_type_metadata,
            "provider_i18n_translations": provider_i18n_translations,
            "provider_type_metadata": provider_type_metadata,
        }

    async def _get_plugin_config(self, plugin_name: str):
        ret: dict = {"metadata": None, "config": None}

        for plugin_md in star_registry:
            if plugin_md.name == plugin_name:
                if not plugin_md.config:
                    break
                ret["config"] = (
                    plugin_md.config
                )  # 杩欐槸鑷畾涔夌殑 Dict 绫伙紙AstrBotConfig锛?
                ret["metadata"] = {
                    plugin_name: {
                        "description": f"{plugin_name} 閰嶇疆",
                        "type": "object",
                        "items": plugin_md.config.schema,  # 鍒濆鍖栨椂閫氳繃 __setattr__ 瀛樺叆浜?schema
                    },
                }
                break

        return ret

    async def _save_astrbot_configs(
        self, post_configs: dict, conf_id: str | None = None
    ) -> None:
        try:
            if conf_id not in self.acm.confs:
                raise ValueError(f"閰嶇疆鏂囦欢 {conf_id} 涓嶅瓨鍦?)
            astrbot_config = self.acm.confs[conf_id]

            # 淇濈暀鏈嶅姟绔殑 t2i_active_template 鍊?
            if "t2i_active_template" in astrbot_config:
                post_configs["t2i_active_template"] = astrbot_config[
                    "t2i_active_template"
                ]

            save_config(post_configs, astrbot_config, is_core=True)
        except Exception as e:
            raise e

    async def _save_plugin_configs(self, post_configs: dict, plugin_name: str) -> None:
        md = None
        for plugin_md in star_registry:
            if plugin_md.name == plugin_name:
                md = plugin_md

        if not md:
            raise ValueError(f"鎻掍欢 {plugin_name} 涓嶅瓨鍦?)
        if not md.config:
            raise ValueError(f"鎻掍欢 {plugin_name} 娌℃湁娉ㄥ唽閰嶇疆")
        assert md.config is not None

        try:
            errors, post_configs = validate_config(
                post_configs, getattr(md.config, "schema", {}), is_core=False
            )
            if errors:
                raise ValueError(f"鏍煎紡鏍￠獙鏈€氳繃: {errors}")
            md.config.save_config(post_configs)
        except Exception as e:
            raise e

