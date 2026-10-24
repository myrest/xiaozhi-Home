const FUNCTION_CALL_LLM_TYPES = new Set(["openai", "ollama", "gemini"]);

export function supportsFunctionCall(llmType) {
  return FUNCTION_CALL_LLM_TYPES.has(llmType);
}
