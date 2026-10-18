"""
Purpose: Presentation-only masking helpers for the human-facing CRM console.
Architecture/Context: The CRM console is used by human contact-center agents. The original HybridAI
                      CRM partially obscured a few fields on screen (phone middle digits in the
                      queue, SSN/card last-4, invoice statement), and this module keeps that UI
                      behaviour unchanged. It is NOT part of the AI data path: MCP tool responses
                      are returned raw and de-identified by Apigee + Model Armor + SDP. These
                      helpers were moved out of src/core/security.py so that the MCP server has
                      no masking logic at all.
Dependencies/Side Effects: Pure functions; no metrics, no I/O.
"""

import re
from typing import Optional


def mask_subscriber_name(name: Optional[str]) -> str:
    """
    Summary:
        Obscures a personal name for on-screen display. Example: 'Elena Rostova' -> 'E**** R******'.

    Parameters:
        name (Optional[str]): Raw subscriber name.

    Return Value:
        str: Masked name, or '[MASKED_SUBSCRIBER]' when empty.

    Exceptions/Errors:
        None.
    """
    if not name or not name.strip():
        return "[MASKED_SUBSCRIBER]"
    return " ".join(part[0] + "*" * max(len(part) - 1, 1) for part in name.strip().split())


def mask_email(email: Optional[str]) -> str:
    """
    Summary:
        Obscures the mailbox and domain of an email. Example: 'elena.rostova@example.com' -> 'e***@***.com'.

    Parameters:
        email (Optional[str]): Raw email address.

    Return Value:
        str: Masked email, or 'N/A' when empty.

    Exceptions/Errors:
        None.
    """
    if not email or not email.strip():
        return "N/A"
    raw = email.strip()
    if "@" not in raw:
        return "***@***.com"
    local_part, domain = raw.split("@", 1)
    masked_local = (local_part[0] + "***") if len(local_part) > 1 else "***"
    domain_parts = domain.split(".")
    masked_domain = f"***.{domain_parts[-1]}" if len(domain_parts) > 1 else "***.com"
    return f"{masked_local}@{masked_domain}"


def mask_phone(phone: Optional[str]) -> str:
    """
    Summary:
        Obscures middle digits of a phone number. Example: '+15552345678' -> '+1 (555) ***-5678'.

    Parameters:
        phone (Optional[str]): Raw phone number.

    Return Value:
        str: Masked phone number, or 'N/A' when empty.

    Exceptions/Errors:
        None.
    """
    if not phone:
        return "N/A"
    digits = re.sub(r"\D", "", phone)
    # Strip the North American country code so 10- and 11-digit inputs render identically.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 10:
        return f"+1 ({digits[:3]}) ***-{digits[-4:]}"
    return f"***-{phone[-4:]}" if len(phone) >= 4 else "****"


def mask_ssn(ssn: Optional[str]) -> str:
    """
    Summary:
        Shows only the last four SSN digits. Example: '123-45-6789' -> '***-**-6789'.

    Parameters:
        ssn (Optional[str]): Raw SSN.

    Return Value:
        str: Masked SSN, or 'N/A' when empty.

    Exceptions/Errors:
        None.
    """
    if not ssn:
        return "N/A"
    clean = re.sub(r"\D", "", ssn)
    return f"***-**-{clean[-4:]}" if len(clean) >= 4 else "***-**-****"


def mask_credit_card(card_num: Optional[str]) -> str:
    """
    Summary:
        PCI-DSS style display of a card number. Example: '4111222233334444' -> '****-****-****-4444'.

    Parameters:
        card_num (Optional[str]): Raw card number.

    Return Value:
        str: Masked card, or 'N/A' when empty.

    Exceptions/Errors:
        None.
    """
    if not card_num:
        return "N/A"
    digits = re.sub(r"\D", "", card_num)
    return f"****-****-****-{digits[-4:]}" if len(digits) >= 4 else "****-****-****-****"


def mask_street_address(address: Optional[str]) -> str:
    """
    Summary:
        Keeps house number and street type, hides the street name.
        Example: '742 Evergreen Terrace, Apt 4B' -> '742 ****** 4B'.

    Parameters:
        address (Optional[str]): Raw street address.

    Return Value:
        str: Partially masked address, or 'N/A' when empty.

    Exceptions/Errors:
        None.
    """
    if not address:
        return "N/A"
    tokens = address.split(" ")
    if len(tokens) <= 2:
        return f"{tokens[0]} [MASKED]"
    return f"{tokens[0]} {'*' * 6} {tokens[-1]}"
