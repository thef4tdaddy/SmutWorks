"""Compact ASCII branding that preserves working space on smaller terminals."""


def header(width, height, subtitle=""):
    if width >= 90 and height >= 28:
        return [
            "  /==/==/==/   S M U T W O R K S",
            " /__/__/__/    MEDIA WORKSHOP",
            " |   >    |    " + subtitle,
            " +--------+",
        ]
    return ["SMUTWORKS  /  MEDIA WORKSHOP", subtitle]
