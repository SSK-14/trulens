# ruff: noqa: E401, E402, F401, F403
"""
!!! warning
    This module is deprecated and will be removed. Use
    `trulens.providers.langchain.endpoint` instead.
"""

from trulens.core.utils import deprecation as deprecation_utils

deprecation_utils.packages_dep_warn()

from trulens.providers.langchain.endpoint import LangchainCallback
from trulens.providers.langchain.endpoint import LangchainEndpoint
