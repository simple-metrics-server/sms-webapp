"""Drop-in folder for metric handlers.

Each module in this package must define exactly one MetricHandler
subclass. The module name becomes the handler name; the runtime
activates it if a matching config/<name>.json exists.
"""
