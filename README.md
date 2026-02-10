# Usage Instructions

## Local Run

To run the application locally, ensure you have the required dependencies installed. Use the following command:

```bash
pip install -r requirements.txt
```

Then, you can run the application with:

```bash
python main.py
```

## Environment Variables

Ensure the following environment variables are set before running the application:
- `API_KEY`: Your API key for external services.
- `DATABASE_URL`: The URL to your database.

## OpenAI Chat Completions

Example usage for OpenAI Chat Completions:

```python
import openai

response = openai.ChatCompletion.create(
  model='gpt-4',
  messages=[{'role': 'user', 'content': 'Hello!'}]
)
```

## Anthropic Messages/Claude Code

Example usage for Anthropic Messages:

```python
import anthropic

response = anthropic.Completion.create(
  model='claude-v1',
  prompt='Hello!'
)
```

## events.jsonl Description

events.jsonl contains logs of events for tracking interactions, including request and response data for API calls. Each line represents a unique event with its timestamp, type, and data.

For more detailed information, please refer to the documentation provided with your SDK or API.