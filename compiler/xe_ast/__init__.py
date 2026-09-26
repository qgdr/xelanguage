"""公开前端接口。解析只建立源码结构，不执行程序或检查所有权。"""
from .parser import parse_source
from .source import Diagnostic, Source
from .semantic import check_source

__all__ = ["parse_source", "check_source", "Diagnostic", "Source"]
