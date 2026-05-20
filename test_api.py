from config.settings import ANTHROPIC_API_KEY
import anthropic

c = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
r = c.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=100,
    messages=[{"role": "user", "content": "Say hello in Hebrew"}]
)
print(r.content[0].text)