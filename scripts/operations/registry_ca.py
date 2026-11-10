# SPDX-License-Identifier: MIT
"""Accept certificate-only PEM chains; never distribute a combined private key file."""
import re
import ssl

from .models import OperationError


def public_ca(raw):
    if not isinstance(raw,bytes) or len(raw)>64*1024:
        raise OperationError('ops.registry.certificate')
    if not raw:return raw
    try:
        pem=raw.decode('ascii')
        pattern=r'(?:\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+'
        if not re.fullmatch(pattern,pem):raise ValueError()
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
    except (ValueError,UnicodeError,ssl.SSLError):
        raise OperationError('ops.registry.certificate') from None
    return raw
