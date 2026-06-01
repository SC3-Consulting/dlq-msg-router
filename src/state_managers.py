import dbm
import json
import time
import threading
import logging
import os
from cachetools import TTLCache

class IdempotencyStore:
    """
    Disk-backed, thread-safe key-value store using Python's native dbm.
    Survives container restarts to prevent mass duplicate processing.
    """
    def __init__(self, db_path=None):
        # Parameterise DB path with .env fallback
        self.db_path = db_path or os.getenv("IDEMPOTENCY_DB_PATH", "data/idempotency.db")
        self.lock = threading.Lock()
        self.logger = logging.getLogger("IdempotencyStore")
        
        # CRITICAL PATCH: Ensure parent directory exists before dbm tries to create the file
        db_dir = os.path.dirname(self.db_path)
        if db_dir:  # Only attempt to create if a directory path actually exists
            os.makedirs(db_dir, exist_ok=True)
        
    def increment(self, hash_key: str, ttl_seconds: int = 86400) -> int:
        """Increments the occurrence count for a hash and updates its TTL."""
        with self.lock:
            try:
                with dbm.open(self.db_path, 'c') as db:
                    now = int(time.time())
                    count = 0
                    
                    key_bytes = hash_key.encode('utf-8')
                    if key_bytes in db:
                        try:
                            data = json.loads(db[key_bytes].decode('utf-8'))
                            if data.get("expires_at", 0) > now:
                                count = data.get("count", 0)
                        except (json.JSONDecodeError, ValueError):
                            # Corrupted or legacy format, gracefully reset
                            pass 
                            
                    count += 1
                    db[key_bytes] = json.dumps({
                        "count": count,
                        "expires_at": now + ttl_seconds
                    }).encode('utf-8')
                    
                    return count
            except Exception as e:
                # CRITICAL PATCH: Fail-Open Protocol. 
                # If OS-level file permissions fail or DB corrupts, degrade gracefully.
                # Do not halt the main triage pipeline over a side-cache failure.
                self.logger.error(f"Idempotency DB unavailable: {str(e)}. Failing OPEN.")
                return 1

    def cleanup_expired(self):
        """Sweeps the dbm file to purge expired keys and prevent infinite disk leak."""
        # TODO: [Tech Debt] Global lock contention. 
        # This holds the thread lock during the full dictionary iteration.
        # Acceptable for MVP, but migrate to a concurrent DB (e.g., Redis/Cosmos) for high-scale prod.
        with self.lock:
            try:
                with dbm.open(self.db_path, 'c') as db:
                    now = int(time.time())
                    keys_to_delete = []
                    
                    for key in db.keys():
                        try:
                            data = json.loads(db[key].decode('utf-8'))
                            if data.get("expires_at", 0) <= now:
                                keys_to_delete.append(key)
                        except Exception:
                            keys_to_delete.append(key) # Delete corrupted keys
                            
                    for key in keys_to_delete:
                        del db[key]
                        
                    if keys_to_delete:
                        self.logger.info(f"Purged {len(keys_to_delete)} expired keys from Idempotency Store.")
            except Exception as e:
                self.logger.error(f"Failed to execute idempotency cleanup: {e}")

class ClassificationCache:
    """
    Memory-backed, thread-safe LRU cache with strict TTL enforcement.
    Used for high-speed deterministic pattern matching.
    """
    def __init__(self, maxsize: int = 10000, ttl_seconds: int = None):
        # CRITICAL PATCH: Strictly enforce environment variable precedence
        env_ttl = int(os.getenv("CLASSIFICATION_TTL_SECONDS", 600))
        active_ttl = ttl_seconds if ttl_seconds is not None else env_ttl
        
        self.cache = TTLCache(maxsize=maxsize, ttl=active_ttl)
        self.lock = threading.Lock()
        
    def get(self, key: str):
        with self.lock:
            return self.cache.get(key)
            
    def save(self, key: str, value: dict, ttl_seconds: int = None):
        # TTLCache manages expiry globally based on init. 
        # The ttl_seconds parameter is accepted for interface compatibility but managed by cachetools.
        with self.lock:
            self.cache[key] = value
            
    def exists(self, key: str) -> bool:
        with self.lock:
            return key in self.cache

    def flush(self):
        with self.lock:
            self.cache.clear()