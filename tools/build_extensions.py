"""Build the unpacked, fixed-ID extensions. Only public keys are distributed."""
from pathlib import Path
import json
import shutil

ROOT = Path(__file__).resolve().parent.parent
KEYS = {
    "Edge": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApXUL4eOAcRugm8nhUy2RtbssRauRHDolHjWaqa+Zo/P9pU4RyIKNo0iaYLbZSgs6I2EQrY+6MM124ILgRDdh/6+DxsNk+V0cyME40bkGVq9I3AK6IJSBeQyrNU6CXMb/6w9UG0PXWW8dpOMCeYZzKydehwLYxSbhsNAdAd0vvPQgzyfQ+r0v+APeezsDSulWW5qmPJzJ0QEO8mpbNvLHLiyS2mGgN3T/nOMW4Ame8y8ayTa8C4REjHEbIiscNbrwsG4ZzrI7fAQMKS5rJUPjsTvO20/NC6iEaIrwbtguKEVQKL3yy+eQexcGSSRML4hg47fOIhap5blQ59jy0d92GQIDAQAB",
    "Chrome": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqMFfp0QmHszQTSkjx/vWn2yuAC7qjIjox0u59WVWoaFtovC1Dn4xAn+jQVMcQU4TWmovwIUE4PFV4+RAVEOBTVbMMyUCQoyRCpjj0WqHfyHezewfKRJkcYS5ljaQ+zMTN7slEYK6hRkwn2KAiI/eHJwBiQXOOa0hu29knzsdW+IzmaxQoN12zxSl1AnyUT6vlCViiuvMzXHXyLD/NFLoFQqaOm0jeHEdb1Si1775vbpJlZqeMXlgHSmGRnTI1rFfiowoBBBRUKVKcctKwgWbxIbzRcj3ZTtuAXEvqZkw2sqxiOtQ35OLlUm7H7p22ysDiP4/AJvcU8n4XzepwDv6JQIDAQAB",
}


def build():
    for browser, key in KEYS.items():
        target = ROOT / "extensions" / browser.lower()
        target.mkdir(parents=True, exist_ok=True)
        for source in (ROOT / "extensions/common").iterdir():
            if source.is_file():
                shutil.copyfile(source, target / source.name)
        (target / "role.js").write_text(f'"use strict";\nconst BROWSER_KIND = "{browser}";\n', encoding="utf-8")
        manifest = {"manifest_version": 3, "name": f"本机浏览器迁移 · {browser}",
                    "version": "0.1.0", "description": "与本机 Python 桌面工具配合进行双向数据迁移。",
                    "minimum_chrome_version": "132", "key": key, "incognito": "not_allowed",
                    "permissions": ["bookmarks", "cookies", "storage", "nativeMessaging"],
                    "host_permissions": ["http://*/*", "https://*/*"],
                    "action": {"default_title": "浏览器迁移", "default_popup": "bridge.html"}}
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    build()
