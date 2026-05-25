import time

class InMemoryCache:
    def increment(self, key, ttl_seconds=None):
        """Increments the counter for a key and returns the new count."""
        self._purge_if_expired(key)
        if key in self.store:
            self.store[key]['count'] += 1
        else:
            expiration = time.time() + (ttl_seconds or self.default_ttl)
            # Initialize with count 1
            self.store[key] = {'value': 'processed', 'expires_at': expiration, 'count': 1}
        return self.store[key]['count']
    
    def __init__(self, default_ttl_seconds=3600):
        self.store = {}
        self.default_ttl = default_ttl_seconds

    def exists(self, key):
        self._purge_if_expired(key)
        return key in self.store

    def get(self, key):
        self._purge_if_expired(key)
        item = self.store.get(key)
        return item['value'] if item else None

    def save(self, key, value="processed", ttl_seconds=None):
        expiration = time.time() + (ttl_seconds or self.default_ttl)
        self.store[key] = {'value': value, 'expires_at': expiration}

    def flush(self):
        self.store.clear()

    def _purge_if_expired(self, key):
        if key in self.store:
            if time.time() > self.store[key]['expires_at']:
                del self.store[key]