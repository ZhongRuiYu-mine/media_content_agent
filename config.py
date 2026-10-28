# coding: utf-8
"""config.py - 配置加载(密钥治理: 走 .env / 环境变量)。

只保留真正被读取的配置: LLM 端点与部门文风维度。成稿/平台旋钮
统一在 content/content_engine.py 与 content/platform_spec.py(env 可覆盖), 不在此重复定义。

**为什么这里要给 openjiuwen 兜底**: 这个配置对象在两处用到 —— ① 内容域自己的
"要不要调大模型"判断; ② 真调大模型时构造请求参数。②只有装了 LLM 运行时(openjiuwen)
才可能发生, ①完全不需要。所以没装 openjiuwen 时用一份字段相同的本地 dataclass 顶上,
让 **确定性链路(核验/缺口/成稿/渲染/预览/六角色辅助)在一台只有 starlette 的机器上也能跑**。
"""
import os
from dataclasses import dataclass

try:                                   # 装了 openjiuwen(能走大模型)就用它
    from openjiuwen.core.foundation.llm import ModelRequestConfig, ModelClientConfig
    LLM_RUNTIME_AVAILABLE = True
except ImportError:                    # 只跑确定性链路: 用字段相同的本地占位
    LLM_RUNTIME_AVAILABLE = False

    @dataclass
    class ModelClientConfig:           # noqa: D101 - 仅占位, 字段与 openjiuwen 一致
        client_provider: str = "openai"
        api_key: str = ""
        api_base: str = ""
        verify_ssl: bool = False

    @dataclass
    class ModelRequestConfig:          # noqa: D101
        model: str = ""
        temperature: float = 0.7
        max_tokens: int = 8192


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 加载器（无第三方依赖）。已存在的环境变量优先。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


@dataclass
class LLMConfig:
    provider: str = "openai"
    api_key: str = ""
    api_base: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    verify_ssl: bool = False
    temperature: float = 0.7
    max_tokens: int = 8192

    def to_client_config(self) -> ModelClientConfig:
        return ModelClientConfig(
            client_provider=self.provider,
            api_key=self.api_key,
            api_base=self.api_base,
            verify_ssl=self.verify_ssl,
        )

    def to_request_config(self) -> ModelRequestConfig:
        return ModelRequestConfig(
            model=self.model_name,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


@dataclass
class HouseStyleConfig:
    dimensions: list = None

    def __post_init__(self):
        if self.dimensions is None:
            self.dimensions = ["事实准确", "表达清楚", "平台适配", "风险可控"]


@dataclass
class AgentConfig:
    llm: LLMConfig = None
    house_style: HouseStyleConfig = None          # 字段名保留(内部标识符不改, 见 design/11 §13 C9)
    agent_id: str = "media_content_agent"
    agent_version: str = "1.0.0"
    agent_description: str = ("新媒体信宣智能体 - 把社团活动素材变成可直接送审的稿件, "
                              "每句话都能点回原素材")

    def __post_init__(self):
        if self.llm is None:
            self.llm = LLMConfig()
        if self.house_style is None:
            self.house_style = HouseStyleConfig()


def load_config_from_env() -> AgentConfig:
    """从环境变量/.env 加载配置。API_KEY 缺失时为空串, 由调用方判断。"""
    dims_env = os.getenv("HOUSE_STYLE_DIMENSIONS", "").strip()
    return AgentConfig(
        llm=LLMConfig(
            provider=os.getenv("MODEL_PROVIDER", "openai"),
            api_key=os.getenv("API_KEY", ""),
            api_base=os.getenv("API_BASE", "https://api.deepseek.com"),
            model_name=os.getenv("MODEL_NAME", "deepseek-v4-flash"),
            verify_ssl=os.getenv("LLM_SSL_VERIFY", "false").lower() == "true",
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
        ),
        house_style=HouseStyleConfig(
            dimensions=[d.strip() for d in dims_env.split(",")] if dims_env else None
        ),
    )


DEFAULT_CONFIG = load_config_from_env()


def build_agent_config(
    user_config: dict,
    default: AgentConfig = None,
) -> AgentConfig:
    """把用户自定义配置合并到默认配置(用户字段优先)。

    仅合并 LLM 端点字段; 部门文风维度沿用默认; 成稿/平台旋钮不在此配置。
    """
    if default is None:
        default = DEFAULT_CONFIG
    return AgentConfig(
        llm=LLMConfig(
            provider=user_config.get("provider") or default.llm.provider,
            api_key=user_config.get("api_key") or default.llm.api_key,
            api_base=user_config.get("api_base") or default.llm.api_base,
            model_name=user_config.get("model_name") or default.llm.model_name,
            verify_ssl=user_config.get("verify_ssl", default.llm.verify_ssl),
            temperature=default.llm.temperature,
            max_tokens=default.llm.max_tokens,
        ),
        house_style=default.house_style,
        agent_description=default.agent_description,
    )
