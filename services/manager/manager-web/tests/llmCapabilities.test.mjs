import { describe, expect, it } from 'vitest';
import { supportsFunctionCall } from '../src/utils/llmCapabilities.js';

describe('supportsFunctionCall', () => {
  it.each(['openai', 'ollama', 'gemini'])('allows %s providers', (llmType) => {
    expect(supportsFunctionCall(llmType)).toBe(true);
  });

  it.each(['dify', 'fastgpt', 'homeassistant', 'unknown', '', null])(
    'rejects %s providers',
    (llmType) => {
      expect(supportsFunctionCall(llmType)).toBe(false);
    },
  );
});
