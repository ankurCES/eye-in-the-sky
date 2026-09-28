"""The analyst's model-provider catalog (BYOK spec v1, section 1). Data only.

Every entry describes how the bundled Claude Code CLI reaches one provider
through environment variables on its child process. The mechanics (which
variable gets which value, validation, the secret store) live in
``llm_settings``; this module holds only facts, each with its source in
``docs``. Anything not confirmed in the provider's own documentation, or by
the offline plumbing runs, carries an ``Unverified:`` note.

Entry shape (``ProviderSpec``)::

    id            stable [a-z0-9_]+ id
    label         operator-facing name, sentence case
    kind          anthropic_login | anthropic_key | anthropic_compatible |
                  bedrock | vertex | foundry | custom
    group         claude | cloud | compatible | custom      (UI grouping)
    model_family  claude | non_claude | mixed
    claude_prefixes   (mixed only) model id prefixes that mean a Claude model
    base_url      {default, editable, required, presets:[{label, url}]}
    auth          {scheme, schemes, env_override, key_label, key_optional,
                   default_token}
                  scheme: login | x-api-key | bearer | none | cloud
                  key_optional: False | True | "loopback" (optional only for a
                  loopback endpoint)
    models        {default, small_default, suggestions, placeholder}
                  default None = the chat module's DEFAULT_MODEL (Claude kinds)
                  or "the operator must type one" (free-text kinds)
    fields        [{id, label, env, required, pattern, placeholder, default, type}]
                  type: text | file.  env None = used only as a {placeholder}
                  in the base URL.
    thinking      adaptive | auto | off    (auto: adaptive until the full check
                  sees the provider reject it)
    effort        whether ``effort`` may be passed (Claude kinds only)
    cost_basis    anthropic_list | unreliable
    quick_check   None, or {kind: models_list | key_info | messages, path,
                  billable}
    extra_env     vendor-documented variables for this provider
    notes         operator-facing caveats (sentence case)
    docs_url      the primary guide; ``docs`` lists every source used
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

FACTS_CHECKED = "2026-09-27"

KINDS = ("anthropic_login", "anthropic_key", "anthropic_compatible", "bedrock", "vertex",
         "foundry", "custom")
GROUPS = ("claude", "cloud", "compatible", "custom")
FAMILIES = ("claude", "non_claude", "mixed")
THINKING_MODES = ("adaptive", "auto", "off")
AUTH_SCHEMES = ("login", "x-api-key", "bearer", "none", "cloud")
COST_BASES = ("anthropic_list", "unreliable")
QUICK_CHECKS = ("models_list", "key_info", "messages")

#: Kinds that accept ``effort`` (spec section 2).
EFFORT_KINDS = frozenset({"anthropic_login", "anthropic_key", "bedrock", "vertex", "foundry"})
#: Kinds that run with the HARDEN set and an isolated CLAUDE_CONFIG_DIR.
BYOK_KINDS = frozenset(set(KINDS) - {"anthropic_login"})
#: Kinds that talk the Anthropic Messages format to a base URL.
URL_KINDS = frozenset({"anthropic_compatible", "custom"})
CLOUD_KINDS = frozenset({"bedrock", "vertex", "foundry"})

DOCS_LLM_GATEWAY = "https://code.claude.com/docs/en/llm-gateway"
#: Shown on every non_claude or mixed entry (spec section 1.3).
NON_CLAUDE_NOTE = ("Anthropic doesn't support routing Claude Code to non-Claude models "
                   "through any gateway.")
#: Shown under the model field of every anthropic_compatible entry.
COMPAT_BILLING_NOTE = "Costs aren't shown for this provider: it bills you directly."

#: Variables vendors document for their Claude Code setups that the catalog may
#: set through ``extra_env``. Anything else is rejected by the catalog tests.
EXTRA_ENV_ALLOWLIST = frozenset({"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "API_TIMEOUT_MS"})
#: Variables a catalog field may map to.
FIELD_ENV_ALLOWLIST = frozenset({
    "AWS_REGION", "AWS_PROFILE", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_FOUNDRY_RESOURCE",
})

CLAUDE_SUGGESTIONS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")

_AWS_REGION = r"^[a-z]{2}(-gov)?-[a-z]+-\d$"
_AWS_PROFILE = r"^[A-Za-z0-9_.+-]{1,64}$"
_GCP_PROJECT = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
_VERTEX_REGION = r"^(global|us|eu|[a-z]+-[a-z]+\d)$"
_FOUNDRY_RESOURCE = r"^[A-Za-z0-9][A-Za-z0-9-]{1,62}$"
_ALIBABA_WORKSPACE = r"^[A-Za-z0-9-]{1,64}$"


def _base(default: str | None = None, *, editable: bool = False, required: bool = True,
          presets: tuple[tuple[str, str], ...] = ()) -> dict:
    return {"default": default, "editable": editable, "required": required,
            "presets": tuple({"label": label, "url": url} for label, url in presets)}


def _auth(scheme: str, *, env: tuple[str, ...] = (), key_label: str | None = None,
          key_optional: bool | str = False, default_token: str | None = None,
          schemes: tuple[str, ...] | None = None) -> dict:
    return {"scheme": scheme, "schemes": schemes or (scheme,), "env_override": env,
            "key_label": key_label, "key_optional": key_optional,
            "default_token": default_token}


def _models(default: str | None, small: str | None = None,
            suggestions: tuple[str, ...] = (), placeholder: str | None = None) -> dict:
    return {"default": default, "small_default": small, "suggestions": suggestions,
            "placeholder": placeholder}


def _field(fid: str, label: str, env: str | None, *, required: bool, pattern: str | None,
           placeholder: str | None = None, default: str | None = None,
           type: str = "text") -> dict:
    return {"id": fid, "label": label, "env": env, "required": required, "pattern": pattern,
            "placeholder": placeholder, "default": default, "type": type}


def _docs(*pairs: tuple[str, str]) -> tuple[dict, ...]:
    return tuple({"label": label, "url": url} for label, url in pairs)


_MESSAGES_CHECK = {"kind": "messages", "path": "/v1/messages", "billable": True}

_PROVIDERS: tuple[dict, ...] = (
    # ------------------------------------------------------------ 1.1 Claude
    {
        "id": "anthropic_login",
        "label": "Claude login (this Mac)",
        "kind": "anthropic_login",
        "group": "claude",
        "model_family": "claude",
        "base_url": None,
        "auth": _auth("login", env=("CLAUDE_CODE_OAUTH_TOKEN",)),
        "models": _models(None, None, CLAUDE_SUGGESTIONS),
        "fields": (),
        "thinking": "adaptive",
        "effort": True,
        "cost_basis": "anthropic_list",
        "quick_check": None,
        "extra_env": {},
        "notes": (
            "Uses the Claude login saved on this Mac by the claude CLI (/login).",
            ("For your own local use only: third-party products may not offer a claude.ai "
            "login without Anthropic's approval."),
        ),
        "docs_url": "https://code.claude.com/docs/en/authentication",
        "docs": _docs(("Authentication", "https://code.claude.com/docs/en/authentication"),
                      ("Agent SDK overview",
                       "https://code.claude.com/docs/en/agent-sdk/overview")),
    },
    {
        "id": "anthropic_api",
        "label": "Anthropic API key",
        "kind": "anthropic_key",
        "group": "claude",
        "model_family": "claude",
        "base_url": _base("https://api.anthropic.com"),
        "auth": _auth("x-api-key", env=("ANTHROPIC_API_KEY",),
                      key_label="Anthropic API key"),
        "models": _models(None, None, (*CLAUDE_SUGGESTIONS, "claude-fable-5-1")),
        "fields": (),
        "thinking": "adaptive",
        "effort": True,
        "cost_basis": "anthropic_list",
        "quick_check": {"kind": "models_list", "path": "/v1/models?limit=1", "billable": False},
        "extra_env": {},
        "notes": ("The supported way to run the analyst with your own Anthropic account.",),
        "docs_url": "https://code.claude.com/docs/en/agent-sdk/quickstart",
        "docs": _docs(
            ("Agent SDK quickstart", "https://code.claude.com/docs/en/agent-sdk/quickstart"),
            ("Models overview",
             "https://platform.claude.com/docs/en/about-claude/models/overview"),
            ("List models", "https://platform.claude.com/docs/en/api/models-list")),
    },
    # ------------------------------------------------------- 1.2 Claude cloud
    {
        "id": "bedrock",
        "label": "Amazon Bedrock",
        "kind": "bedrock",
        "group": "cloud",
        "model_family": "claude",
        "base_url": None,
        "auth": _auth("cloud", env=("AWS_BEARER_TOKEN_BEDROCK",),
                      key_label="Bedrock API key", key_optional=True),
        "models": _models("us.anthropic.claude-opus-5-5",
                          "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        "fields": (
            _field("region", "AWS region", "AWS_REGION", required=True, pattern=_AWS_REGION,
                   placeholder="us-east-1"),
            _field("profile", "AWS profile", "AWS_PROFILE", required=False,
                   pattern=_AWS_PROFILE, placeholder="default"),
        ),
        "thinking": "adaptive",
        "effort": True,
        "cost_basis": "anthropic_list",
        "quick_check": None,
        "extra_env": {},
        "notes": (
            "Without a Bedrock API key the default AWS credential chain is used.",
            "Only the full check is available for cloud providers.",
        ),
        "docs_url": "https://code.claude.com/docs/en/amazon-bedrock",
        "docs": _docs(("Amazon Bedrock", "https://code.claude.com/docs/en/amazon-bedrock")),
    },
    {
        "id": "vertex",
        "label": "Google Cloud Agent Platform (Vertex AI)",
        "kind": "vertex",
        "group": "cloud",
        "model_family": "claude",
        "base_url": None,
        "auth": _auth("cloud"),
        "models": _models("claude-opus-5-5", "claude-haiku-4-5@20251001"),
        "fields": (
            _field("project", "Google Cloud project", "ANTHROPIC_VERTEX_PROJECT_ID",
                   required=True, pattern=_GCP_PROJECT, placeholder="my-project-123"),
            _field("region", "Region", "CLOUD_ML_REGION", required=False,
                   pattern=_VERTEX_REGION, placeholder="global", default="global"),
            _field("credentials_file", "Credentials file", "GOOGLE_APPLICATION_CREDENTIALS",
                   required=False, pattern=None, placeholder="/path/to/credentials.json",
                   type="file"),
        ),
        "thinking": "adaptive",
        "effort": True,
        "cost_basis": "anthropic_list",
        "quick_check": None,
        "extra_env": {},
        "notes": (
            "Without a credentials file, gcloud application-default credentials are used.",
            ("A model without a global endpoint needs a regional override, which isn't "
            "offered here yet."),
            "Only the full check is available for cloud providers.",
        ),
        "docs_url": "https://code.claude.com/docs/en/google-vertex-ai",
        "docs": _docs(("Google Vertex AI", "https://code.claude.com/docs/en/google-vertex-ai")),
    },
    {
        "id": "foundry",
        "label": "Microsoft Foundry",
        "kind": "foundry",
        "group": "cloud",
        "model_family": "claude",
        "base_url": None,
        "auth": _auth("cloud", env=("ANTHROPIC_FOUNDRY_API_KEY",),
                      key_label="Foundry API key", key_optional=True),
        "models": _models("claude-opus-5-5", "claude-haiku-4-5"),
        "fields": (
            _field("resource", "Foundry resource name", "ANTHROPIC_FOUNDRY_RESOURCE",
                   required=True, pattern=_FOUNDRY_RESOURCE, placeholder="my-resource"),
        ),
        "thinking": "adaptive",
        "effort": True,
        "cost_basis": "anthropic_list",
        "quick_check": None,
        "extra_env": {},
        "notes": (
            ("Models are your deployment names. Foundry doesn't check the model at start-up, "
            "so a wrong name fails on the first message."),
            "Without an API key, Microsoft Entra ID (DefaultAzureCredential) is used.",
            "Unverified: Azure's exact rules for resource names.",
            "Only the full check is available for cloud providers.",
        ),
        "docs_url": "https://code.claude.com/docs/en/microsoft-foundry",
        "docs": _docs(("Microsoft Foundry", "https://code.claude.com/docs/en/microsoft-foundry")),
    },
    # ------------------------------------------------- 1.3 Other providers
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "mixed",
        "claude_prefixes": ("anthropic/", "~anthropic/"),
        "base_url": _base("https://openrouter.ai/api"),
        "auth": _auth("bearer", env=("OPENROUTER_API_KEY",), key_label="OpenRouter API key"),
        "models": _models("anthropic/claude-opus-5.5", "anthropic/claude-haiku-4.5", (
            "anthropic/claude-sonnet-5", "~anthropic/claude-opus-latest[1m]",
            "minimax/minimax-m3", "moonshotai/kimi-k3", "z-ai/glm-5.3",
            "deepseek/deepseek-v4-pro", "qwen/qwen3.8-flash", "openai/gpt-6-sol",
            "openai/gpt-6-luna", "google/gemini-3.8-flash", "x-ai/grok-4.7",
            "mistralai/mistral-medium-3-5")),
        "fields": (),
        "thinking": "adaptive",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": {"kind": "key_info", "path": "/v1/key", "billable": False},
        "extra_env": {},
        "notes": (
            ("OpenRouter says Claude Code is only guaranteed to work with the Anthropic "
            "first-party provider."),
            "Test connection checks the key only; the full check runs the model.",
            ("Unverified: whether token counting works through OpenRouter, and whether the "
            "key check uses credit."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration",
        "docs": _docs(
            ("Claude Code guide",
             "https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration"),
            ("Agent SDK guide",
             "https://openrouter.ai/docs/guides/community/anthropic-agent-sdk"),
            ("Get current key",
             "https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key")),
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("https://api.minimax.io/anthropic", presets=(
            ("International", "https://api.minimax.io/anthropic"),
            ("China", "https://api.minimax.cn/anthropic"))),
        "auth": _auth("bearer", env=("MINIMAX_API_KEY",), key_label="MiniMax API key"),
        "models": _models("MiniMax-M3[1m]", "MiniMax-M3[1m]", (
            "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M3.1-Flash-Preview")),
        "fields": (),
        "thinking": "adaptive",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"},
        "notes": (
            "MiniMax-M3.1-Flash-Preview is for Token Plan keys only, and always thinks.",
            "MiniMax ignores context_management, mcp_servers and top_k.",
            ("Unverified: whether MiniMax accepts system messages inside the conversation; "
            "the full check catches it."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://platform.minimax.io/docs/token-plan/claude-code",
        "docs": _docs(
            ("Claude Code (international)",
             "https://platform.minimax.io/docs/token-plan/claude-code"),
            ("Claude Code (China)", "https://platform.minimaxi.com/docs/token-plan/claude-code"),
            ("Anthropic API",
             "https://platform.minimax.io/docs/api-reference/text-anthropic-api")),
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("https://api.deepseek.com/anthropic"),
        "auth": _auth("bearer", env=("DEEPSEEK_API_KEY",), key_label="DeepSeek API key"),
        "models": _models("deepseek-flash[1m]", "deepseek-flash", ("deepseek-v4-pro",)),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432"},
        "notes": (
            ("DeepSeek ignores cache_control and anthropic-beta, and drops the error flag on "
            "tool results: a denied tool still returns its text."),
            ("Unverified: whether DeepSeek accepts adaptive thinking (the full check turns it "
            "off if not) and token counting."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/",
        "docs": _docs(
            ("Claude Code",
             "https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/"),
            ("Anthropic API", "https://api-docs.deepseek.com/guides/anthropic_api/"),
            ("Models", "https://api-docs.deepseek.com/quick_start/pricing/")),
    },
    {
        "id": "moonshot",
        "label": "Moonshot Kimi",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("https://api.moonshot.ai/anthropic", presets=(
            ("International", "https://api.moonshot.ai/anthropic"),
            ("China", "https://api.moonshot.cn/anthropic"))),
        "auth": _auth("bearer", env=("MOONSHOT_API_KEY",), key_label="Moonshot API key"),
        "models": _models("kimi-k3[1m]", "kimi-k2.6", ("kimi-k2.7-code",)),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"},
        "notes": (
            ("kimi-k2.7-code only accepts one thinking mode the analyst doesn't send, so it "
            "fails either way; don't use it for background tasks."),
            ("Unverified: whether kimi-k3 accepts adaptive thinking (the full check turns it "
            "off if not), and prompt-cache writes."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://platform.kimi.ai/docs/guide/claude-code-kimi",
        "docs": _docs(
            ("Claude Code", "https://platform.kimi.ai/docs/guide/claude-code-kimi"),
            ("Claude Code (China)", "https://platform.kimi.com/docs/guide/claude-code-kimi"),
            ("Messages API", "https://platform.kimi.ai/docs/api/messages")),
    },
    {
        "id": "zai",
        "label": "Z.ai GLM",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("https://api.z.ai/api/anthropic"),
        "auth": _auth("bearer", env=("ZAI_API_KEY",), key_label="Z.ai API key"),
        "models": _models("glm-5.3[1m]", "glm-5.3-flash[1m]"),
        "fields": (),
        "thinking": "adaptive",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000", "API_TIMEOUT_MS": "3000000"},
        "notes": (
            "Z.ai maps adaptive thinking to maximum effort.",
            ("The GLM Coding Plan is limited to officially supported tools. Unverified: "
            "whether this app counts, and whether pay-as-you-go keys work on this endpoint."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://docs.z.ai/devpack/tool/claude",
        "docs": _docs(("Claude Code", "https://docs.z.ai/devpack/tool/claude"),
                      ("Models and effort", "https://docs.z.ai/devpack/latest-model"),
                      ("FAQ", "https://docs.z.ai/devpack/faq")),
    },
    {
        "id": "zhipu",
        "label": "Zhipu BigModel (China)",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("https://open.bigmodel.cn/api/anthropic"),
        "auth": _auth("x-api-key", env=("ZHIPU_API_KEY",), key_label="Zhipu API key"),
        "models": _models("glm-5.3", "glm-5.3"),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {},
        "notes": (
            "The same GLM models as Z.ai, served in China.",
            "Unverified: whether Zhipu accepts a bearer token; the key is sent as x-api-key.",
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://docs.bigmodel.cn/cn/guide/develop/claude/introduction",
        "docs": _docs(("Claude API compatibility",
                       "https://docs.bigmodel.cn/cn/guide/develop/claude/introduction")),
    },
    {
        "id": "alibaba",
        "label": "Alibaba Model Studio (Qwen)",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base(None, editable=True, presets=(
            ("Singapore",
             "https://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"),
            ("Virginia", "https://{workspace_id}.us-east-1.maas.aliyuncs.com/apps/anthropic"),
            ("Beijing", "https://{workspace_id}.cn-beijing.maas.aliyuncs.com/apps/anthropic"),
            ("Pay-as-you-go (Beijing)", "https://dashscope.aliyuncs.com/apps/anthropic"),
            ("Coding Plan (international)",
             "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"),
            ("Coding Plan (China)", "https://coding.dashscope.aliyuncs.com/apps/anthropic"),
            ("Token Plan (Singapore)",
             "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"))),
        "auth": _auth("bearer", env=("DASHSCOPE_API_KEY",),
                      key_label="Model Studio API key"),
        "models": _models("qwen3.7-max", "qwen3.6-flash",
                          ("qwen3.8-max", "qwen3.7-plus", "qwen3-coder-plus")),
        "fields": (
            _field("workspace_id", "Workspace ID", None, required=False,
                   pattern=_ALIBABA_WORKSPACE, placeholder="ws-abc123"),
        ),
        "thinking": "off",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {},
        "notes": (
            ("The key type must match the endpoint, otherwise Model Studio answers "
            "401 invalid_api_key."),
            ("Extended thinking is off: Model Studio documents only the enabled and disabled "
            "modes."),
            ("Unverified: the workspace ID format, and how Model Studio handles "
            "anthropic-beta."),
            NON_CLAUDE_NOTE,
            COMPAT_BILLING_NOTE,
        ),
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/claude-code",
        "docs": _docs(
            ("Claude Code", "https://www.alibabacloud.com/help/en/model-studio/claude-code"),
            ("Messages API",
             "https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages")),
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("http://localhost:11434", editable=True, presets=(
            ("Local", "http://localhost:11434"), ("Ollama Cloud", "https://ollama.com"))),
        "auth": _auth("bearer", env=("OLLAMA_API_KEY",), key_label="Ollama API key",
                      key_optional="loopback", default_token="ollama"),
        "models": _models(None, None, (), placeholder="qwen3.5"),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {},
        "notes": (
            "A local server needs no key. Ollama Cloud needs one.",
            ("Ollama has no tool_choice, token counting or prompt caching. Use a model with "
            "at least 64k of context."),
            NON_CLAUDE_NOTE,
        ),
        "docs_url": "https://docs.ollama.com/integrations/claude-code",
        "docs": _docs(("Claude Code", "https://docs.ollama.com/integrations/claude-code"),
                      ("Anthropic compatibility",
                       "https://docs.ollama.com/api/anthropic-compatibility")),
    },
    {
        "id": "lmstudio",
        "label": "LM Studio",
        "kind": "anthropic_compatible",
        "group": "compatible",
        "model_family": "non_claude",
        "base_url": _base("http://localhost:1234", editable=True),
        "auth": _auth("bearer", key_label="LM Studio API token", key_optional=True,
                      default_token="lmstudio"),
        "models": _models(None, None, (), placeholder="openai/gpt-oss-20b"),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {},
        "notes": (
            "Needs LM Studio 0.4.1 or later and a context longer than 25k tokens.",
            "A key is needed only when Require Authentication is on in LM Studio.",
            "Unverified: thinking support.",
            NON_CLAUDE_NOTE,
        ),
        "docs_url": "https://lmstudio.ai/docs/integrations/claude-code",
        "docs": _docs(("Claude Code", "https://lmstudio.ai/docs/integrations/claude-code"),
                      ("Anthropic compatibility",
                       "https://lmstudio.ai/docs/developer/anthropic-compat")),
    },
    # ---------------------------------------------------------------- custom
    {
        "id": "custom",
        "label": "Custom Anthropic-compatible endpoint",
        "kind": "custom",
        "group": "custom",
        "model_family": "non_claude",
        "base_url": _base(None, editable=True),
        "auth": _auth("bearer", key_label="API key", default_token="unused",
                      schemes=("bearer", "x-api-key", "none")),
        "models": _models(None, None, ()),
        "fields": (),
        "thinking": "auto",
        "effort": False,
        "cost_basis": "unreliable",
        "quick_check": _MESSAGES_CHECK,
        "extra_env": {},
        "notes": (
            ("The endpoint must serve POST /v1/messages with streaming. Token counting is "
            "optional."),
            "Enter the API root, without /v1.",
            NON_CLAUDE_NOTE,
        ),
        "docs_url": "https://code.claude.com/docs/en/llm-gateway-connect",
        "docs": _docs(
            ("Connect a gateway", "https://code.claude.com/docs/en/llm-gateway-connect"),
            ("Gateway protocol", "https://code.claude.com/docs/en/llm-gateway-protocol"),
            ("LLM gateways", DOCS_LLM_GATEWAY)),
    },
)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


#: id -> read-only ProviderSpec, in catalog (UI) order.
PROVIDERS: MappingProxyType = MappingProxyType({p["id"]: _freeze(p) for p in _PROVIDERS})
PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS)
DEFAULT_PROVIDER = "anthropic_login"


def get(provider_id: str) -> Any:
    """The spec for ``provider_id``; ``KeyError`` for an unknown id."""
    return PROVIDERS[provider_id]


def key_env_names() -> frozenset[str]:
    """Every launch-environment variable a catalog entry reads its key from."""
    return frozenset(n for p in PROVIDERS.values() for n in p["auth"]["env_override"])


__all__ = [
    "AUTH_SCHEMES", "BYOK_KINDS", "CLAUDE_SUGGESTIONS", "CLOUD_KINDS", "COMPAT_BILLING_NOTE",
    "COST_BASES", "DEFAULT_PROVIDER", "DOCS_LLM_GATEWAY", "EFFORT_KINDS",
    "EXTRA_ENV_ALLOWLIST", "FACTS_CHECKED", "FAMILIES", "FIELD_ENV_ALLOWLIST", "GROUPS",
    "KINDS", "NON_CLAUDE_NOTE", "PROVIDERS", "PROVIDER_IDS", "QUICK_CHECKS",
    "THINKING_MODES", "URL_KINDS", "get", "key_env_names",
]
