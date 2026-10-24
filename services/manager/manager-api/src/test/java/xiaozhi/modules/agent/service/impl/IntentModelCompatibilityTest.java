package xiaozhi.modules.agent.service.impl;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import org.junit.jupiter.api.Test;

class IntentModelCompatibilityTest {
    @Test
    void functionCallIntentAcceptsOpenAiOllamaAndGemini() {
        for (String llmType : List.of("openai", "ollama", "gemini")) {
            assertTrue(IntentModelCompatibility.isCompatible(llmType, "Intent_function_call"), llmType);
        }
    }

    @Test
    void functionCallIntentRejectsUnsupportedLlmTypes() {
        for (String llmType : List.of("dify", "fastgpt", "homeassistant", "unknown")) {
            assertFalse(IntentModelCompatibility.isCompatible(llmType, "Intent_function_call"), llmType);
        }
        assertFalse(IntentModelCompatibility.isCompatible(null, "Intent_function_call"));
    }

    @Test
    void otherIntentModesRemainAllowedForUnsupportedLlmTypes() {
        assertTrue(IntentModelCompatibility.isCompatible("dify", "Intent_nointent"));
        assertTrue(IntentModelCompatibility.isCompatible("dify", null));
    }
}
