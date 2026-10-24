package xiaozhi.modules.agent.service.impl;

import java.util.Set;

final class IntentModelCompatibility {
    private static final String FUNCTION_CALL_INTENT_MODEL_ID = "Intent_function_call";
    private static final Set<String> FUNCTION_CALL_LLM_TYPES = Set.of("openai", "ollama", "gemini");

    private IntentModelCompatibility() {
    }

    static boolean isCompatible(String llmType, String intentModelId) {
        return !FUNCTION_CALL_INTENT_MODEL_ID.equals(intentModelId)
                || (llmType != null && FUNCTION_CALL_LLM_TYPES.contains(llmType));
    }
}
