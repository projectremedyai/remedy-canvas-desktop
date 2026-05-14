export type ProviderId = "local-ollama" | "ollama-cloud" | "openrouter";

export interface ProviderConfig {
  provider: ProviderId;
  text_model: string;
  vision_model: string;
  has_api_key: boolean;
}

export interface ProviderMeta {
  id: ProviderId;
  label: string;
  description: string;
  requiresApiKey: boolean;
  defaultTextModel: string;
  defaultVisionModel: string;
  signupUrl: string;
}

export const PROVIDERS: ProviderMeta[] = [
  {
    id: "local-ollama",
    label: "Bundled (offline)",
    description: "Runs gemma4 on your Mac. Keeps content fully local.",
    requiresApiKey: false,
    defaultTextModel: "",
    defaultVisionModel: "",
    signupUrl: "",
  },
  {
    id: "ollama-cloud",
    label: "Ollama Cloud",
    description:
      "Sends content to ollama.com. Bring your own API key from your Ollama account.",
    requiresApiKey: true,
    defaultTextModel: "gpt-oss:120b",
    defaultVisionModel: "gpt-oss:120b",
    signupUrl: "https://ollama.com/settings/keys",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    description:
      "Routes to 300+ models (OpenAI, Anthropic, etc.) via openrouter.ai. Bring your own API key.",
    requiresApiKey: true,
    defaultTextModel: "openai/gpt-4o",
    defaultVisionModel: "openai/gpt-4o",
    signupUrl: "https://openrouter.ai/keys",
  },
];
