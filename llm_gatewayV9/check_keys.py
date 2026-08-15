import os
from dotenv import load_dotenv
load_dotenv('.env')
keys = ['GEMINI_API_KEY', 'GEMINI_API_KEY_2', 'NVIDIA_API_KEY', 'GROQ_API_KEY',
        'OPENAI_API_KEY', 'GITHUB_TOKEN', 'CEREBRAS_API_KEY', 'OPENROUTER_API_KEY',
        'KILO_API_KEY']
for k in keys:
    v = os.getenv(k)
    status = 'SET' if v else 'NOT SET'
    print(k + ': ' + status)
