#!/usr/bin/env python3
"""
Smoke tests for b23-llm-proxy
Basic validation of key functionality
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Dict, Any

import httpx


class SmokeTest:
    def __init__(self, base_url: str = "http://localhost:8866", admin_token: str = None):
        self.base_url = base_url
        # Use provided token or from environment, no default for security
        self.admin_token = admin_token or os.getenv("ADMIN_TOKEN")
        if not self.admin_token:
            raise ValueError(
                "ADMIN_TOKEN must be set via environment variable or --admin-token argument. "
                "Never use a default token in production."
            )
        self.test_api_key = "test-key-" + str(int(time.time()))
        self.results: Dict[str, bool] = {}
    
    async def run_all_tests(self):
        """Run all smoke tests"""
        print("🧪 Running b23-llm-proxy smoke tests\n")
        print(f"Base URL: {self.base_url}")
        print(f"Admin Token: {'Set' if self.admin_token else 'Not Set'}\n")
        
        tests = [
            ("Health Check", self.test_health_check),
            ("Admin Token Protection", self.test_admin_token_protection),
            ("API Key Upsert", self.test_api_key_upsert),
            ("Invalid API Key", self.test_invalid_api_key),
        ]
        
        passed = 0
        failed = 0
        
        for test_name, test_func in tests:
            try:
                print(f"Running: {test_name}...", end=" ")
                await test_func()
                print("✅ PASSED")
                self.results[test_name] = True
                passed += 1
            except Exception as e:
                print(f"❌ FAILED: {e}")
                self.results[test_name] = False
                failed += 1
        
        print(f"\n{'='*60}")
        print(f"Results: {passed} passed, {failed} failed")
        print(f"{'='*60}\n")
        
        return failed == 0
    
    async def test_health_check(self):
        """Test /healthz endpoint"""
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/healthz")
            assert r.status_code == 200, f"Expected 200, got {r.status_code}"
            data = r.json()
            assert data.get("ok") is True, "Expected ok=True in response"
    
    async def test_admin_token_protection(self):
        """Test that admin endpoints require x-admin-token header"""
        async with httpx.AsyncClient() as client:
            # Test without header
            r = await client.post(
                f"{self.base_url}/admin/keys/upsert",
                json={"api_key": "test", "user_id": "test"}
            )
            assert r.status_code == 403, f"Expected 403 without token, got {r.status_code}"
            
            # Test with wrong token
            r = await client.post(
                f"{self.base_url}/admin/keys/upsert",
                headers={"x-admin-token": "wrong-token"},
                json={"api_key": "test", "user_id": "test"}
            )
            assert r.status_code == 403, f"Expected 403 with wrong token, got {r.status_code}"
    
    async def test_api_key_upsert(self):
        """Test creating/updating an API key"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/admin/keys/upsert",
                headers={"x-admin-token": self.admin_token},
                json={
                    "api_key": self.test_api_key,
                    "user_id": "test-user",
                    "active": True
                }
            )
            assert r.status_code == 200, f"Expected 200, got {r.status_code}"
            data = r.json()
            assert data.get("ok") is True, "Expected ok=True in response"
    
    async def test_invalid_api_key(self):
        """Test that invalid API keys are rejected"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Authorization": "Bearer invalid-key-123"},
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "test"}]
                }
            )
            assert r.status_code == 401, f"Expected 401 for invalid key, got {r.status_code}"


async def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Run smoke tests for b23-llm-proxy")
    parser.add_argument(
        "--url",
        default="http://localhost:8866",
        help="Base URL of the proxy server (default: http://localhost:8866)"
    )
    parser.add_argument(
        "--admin-token",
        help="Admin token for testing (default: from ADMIN_TOKEN env var)"
    )
    args = parser.parse_args()
    
    try:
        tester = SmokeTest(base_url=args.url, admin_token=args.admin_token)
    except ValueError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    
    success = await tester.run_all_tests()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
