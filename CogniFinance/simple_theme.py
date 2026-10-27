"""
simple_theme.py ver 0.1
For CogniFinance ver 1.9

The look shared by the Simple Mode screens (Home, Money, ...): two
palettes, one stylesheet, and the one place money becomes text.
"""
from decimal import Decimal

LIGHT = dict(page="#F3F4F6", card="#FFFFFF", border="#E5E7EB", text="#111827",
             muted="#6B7280", accent="#059669", accent_soft="#ECFDF5",
             negative="#B91C1C", track="#E5E7EB")
DARK = dict(page="#111827", card="#1F2937", border="#374151", text="#F9FAFB",
            muted="#9CA3AF", accent="#34D399", accent_soft="#064E3B",
            negative="#F87171", track="#374151")


def fmt_money(value: Decimal, signed: bool = False) -> str:
    """The one place money becomes text on the Simple screens."""
    if signed:
        sign = "+" if value > 0 else ("-" if value < 0 else "")
    else:
        sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def stylesheet(t: dict, page_id: str) -> str:
    return f"""
        QWidget#{page_id} {{ background: {t['page']}; }}
        QScrollArea {{ background: {t['page']}; }}
        QFrame#HomeCard {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 12px; }}
        QFrame#TxRow {{ background: transparent; border: none; border-radius: 8px; }}
        QFrame#TxRow:hover {{ background: {t['accent_soft']}; }}
        QLabel {{ background: transparent; color: {t['text']}; }}
        QLabel#Greeting {{ font-size: 26px; font-weight: 700; }}
        QLabel#DateLine, QLabel#Muted, QLabel#FieldLabel {{ color: {t['muted']}; font-size: 12px; }}
        QLabel#DayHeading {{ color: {t['muted']}; font-size: 11px; font-weight: 700; padding-top: 8px; }}
        QLabel#Badge {{ color: {t['muted']}; border: 1px solid {t['border']}; border-radius: 6px;
                        padding: 0px 5px; font-size: 10px; }}
        QLabel#CardTitle {{ color: {t['muted']}; font-size: 11px; font-weight: 700; }}
        QLabel#CardInfo {{ color: {t['accent']}; font-size: 13px; }}
        QLabel#BigNumber {{ color: {t['accent']}; font-size: 34px; font-weight: 700; }}
        QPushButton#CardLink {{ color: {t['accent']}; border: none; background: transparent; font-size: 12px; }}
        QPushButton#PrimaryButton {{ background: {t['accent']}; color: white; border: none;
                                     border-radius: 8px; padding: 8px; font-weight: 600; }}
        QPushButton#SecondaryButton {{ background: transparent; color: {t['text']};
                                       border: 1px solid {t['border']}; border-radius: 8px; padding: 8px; }}
        QPushButton#SecondaryButton:hover {{ background: {t['accent_soft']}; }}
        QLineEdit, QComboBox, QDateEdit {{ background: {t['card']}; color: {t['text']};
                                border: 1px solid {t['border']}; border-radius: 8px; padding: 6px; }}
    """
