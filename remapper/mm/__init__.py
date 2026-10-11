from importlib import import_module

BookcastPlugin = import_module( ".bookcast-loop", __name__ ).BookcastLoopPlugin

__all__ = [ "BookcastPlugin" ]
