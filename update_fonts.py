#!/usr/bin/env python3
import re

path = 'templates/index.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace all font references
content = content.replace("'DM Mono'", "'Fira Code'")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✓ Updated all fonts')
