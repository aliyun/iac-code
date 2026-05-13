from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials


class CloudCredentials:
    def __init__(self, aliyun_config_path: str | None = None) -> None:
        self._aliyun_config_path = aliyun_config_path

    def has_provider(self, name: str) -> bool:
        if name == "aliyun":
            return AliyunCredentials.is_configured(config_path=self._aliyun_config_path)
        return False

    def get_provider(self, name: str) -> AliyunCredential | None:
        if name == "aliyun":
            return AliyunCredentials.load(config_path=self._aliyun_config_path)
        return None

    def list_providers(self) -> list[str]:
        result: list[str] = []
        if self.has_provider("aliyun"):
            result.append("aliyun")
        return result
