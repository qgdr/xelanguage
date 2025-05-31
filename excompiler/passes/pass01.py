from parser.node import *


class SymbolTable:
    def __init__(self):
        self.symbols = {}

    def add(self, name, var_type):
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists.")
        self.symbols[name] = var_type

    def get(self, name):
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        return self.symbols.get(name)

    def __contains__(self, name):
        return name in self.symbols

    def __repr__(self):
        return f"SymbolTable({self.symbols})"


class SymbolTableStack:
    def __init__(self):
        self.stack = []

    def push(self):
        self.stack.append(SymbolTable())

    def pop(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name, var_type):
        self.current().add(name, var_type)

    def get(self, name):
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")


symbol_table_stack = SymbolTableStack()


def Function_pass01(self):
    """
    Pass 01: Function pass, used to collect function definitions and their parameters.
    """
    symbol_table_stack.push()  # Push a new symbol table for this function

    for node in self.body:
        node.pass01()

    # After processing all nodes, pop the symbol table
    symbol_table_stack.pop()


# Ensure FunctionNode is a Python class before assigning
setattr(FunctionNode, "pass01", Function_pass01)



def Declaration_pass01(self):
    """
    Pass 01: Declaration pass, used to collect variable declarations.
    """
    for node in self.body:
        if isinstance(node, VarTypePairNode):
            # Add variable to the current symbol table
            symbol_table_stack.add(node.name, node.var_type)
        else:
            node.pass01()