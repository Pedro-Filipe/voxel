from collections import OrderedDict


class LRUCache(OrderedDict):
    def __init__(self, max_items=8):
        super().__init__()
        self.max_items = max_items

    def __setitem__(self, key, value):
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        while len(self) > self.max_items:
            self.popitem(last=False)

    def get(self, key, default=None):
        return super().get(key, default)
