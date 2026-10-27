"""The reviewed core checks parametric templates before monomorphizing them.

The legacy compiler intentionally delays template-body checks until use. Core
must not inherit that policy: an unused template is still a promise about every
admitted substitution. Reuse the ordinary checker with distinct opaque $T types,
not UNKNOWN and not a representative i32/String instantiation. Opaque types have
no Copy/formatting/arithmetic capabilities; normal move and pointer rules apply.

This module does not implement type inference, traits, runtime type objects or
an alternative ownership checker. Admission decides the supported syntax; the
same checker subsequently verifies every actual concrete instance as before.
"""

from typing import TYPE_CHECKING

from .language_policy import current_profile
from .typesys import Type, substitute

if TYPE_CHECKING:
    from .semantic import Checker, Signature


def check_core_templates(checker: "Checker") -> None:
    """Check all original templates once, without populating instance caches.

    Returning addresses of local storage is checked by the same lexical-origin
    rule as a monomorphic body. Template diagnostics are errors even when no
    call site exists. Suppress only speculative warnings: concrete call bodies
    receive the usual independent warning/error pass with real source types.
    """
    if current_profile() != "core":
        return
    templates = [signature for signature in checker.functions.values() if signature.generics]
    previous_flag = checker._checking_core_template
    previous_warnings = checker._report_warnings
    checker._checking_core_template = True
    checker._report_warnings = False
    try:
        for signature in templates:
            checker.check_function(signature)
    finally:
        checker._checking_core_template = previous_flag
        checker._report_warnings = previous_warnings


def abstract_instantiation(signature: "Signature", substitutions: dict[str, Type]) -> "Signature":
    """Apply a signature for an abstract call, never claim a concrete instance.

    For relay[U] calling identity[U], $U must remain opaque. Generating a cached
    function with a $U C layout would incorrectly treat a template as machine
    code. The transient signature deliberately has no return-origin summary:
    calls conservatively preserve all argument origins while templates are
    checked; the ordinary concrete fixed-point pass computes precise summaries.
    """
    # This function runs only after semantic has finished importing. Keeping the
    # import here avoids a semantic -> generic_core -> semantic module cycle.
    from .semantic import Signature

    return Signature(
        signature.node,
        [substitute(parameter, substitutions) for parameter in signature.parameters],
        substitute(signature.result, substitutions),
        self_type=substitute(signature.self_type, substitutions) if signature.self_type else None,
        borrow_parameters=None,
        type_substitutions=substitutions,
    )
