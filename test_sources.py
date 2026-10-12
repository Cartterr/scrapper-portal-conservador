"""
Quick test to find working RUT lookup
"""
import requests
import re

# Test multiple sources with POST request
sources = [
    'https://rutyfirma.com/buscar/',
]

for url in sources:
    print(f"\nTesting: {url}")
    
    # Get page first
    r = requests.get(url)
    print(f"  Status: {r.status_code}")
    
    # Look for form
    form_tag = re.search(r'<form[^>]*>', r.text)
    if form_tag:
        print(f"  Form found: {form_tag.group()[:100]}...")
    
    # Look for search input
    input_tag = re.search(r'<input[^>]*type="text"', r.text)
    if input_tag:
        print(f"  Input found: {input_tag.group()[:100]}...")
    
    # Look for button
    button_tag = re.search(r'<button[^>]*>|<input[^>]*type="submit"', r.text)
    if button_tag:
        print(f"  Button found: {button_tag.group()[:100]}...")
