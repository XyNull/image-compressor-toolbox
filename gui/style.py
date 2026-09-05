"""GUI 深/浅主题 (QSS)。切换按钮在主窗口工具栏。"""
from __future__ import annotations

# 状态色 (两套主题共用, 来自设计稿)
C_OK = "#97C459"
C_WARN = "#EF9F27"
C_ERR = "#F09595"
C_RUN = "#85B7EB"

_DARK = {
    "bg": "#1e1f23", "panel": "#232529", "panel2": "#1a1b1f",
    "border": "#33343a", "border2": "#44464c",
    "text": "#e6e6e6", "muted": "#9a9b9f",
    "accent": "#185FA5", "accent_hover": "#1d6cba", "sel": "#233043",
    "input_bg": "#2b2d33", "handle": "#3a3b40",
    "tab_bg": "#232529", "btn": "#2b2d33",
}

_LIGHT = {
    "bg": "#f2f1ee", "panel": "#ffffff", "panel2": "#fafaf8",
    "border": "#d9d8d2", "border2": "#c0bfb8",
    "text": "#2c2c2a", "muted": "#6f6f6a",
    "accent": "#185FA5", "accent_hover": "#1d6cba", "sel": "#e6f1fb",
    "input_bg": "#ffffff", "handle": "#d9d8d2",
    "tab_bg": "#ecebe7", "btn": "#ecebe7",
}


def _build(t: dict) -> str:
    return f"""
* {{ font-size: 12px; color: {t['text']}; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QWidget#header {{ background: {t['panel']}; border-bottom: 1px solid {t['border']}; }}
QLabel#appTitle {{ font-size: 13px; font-weight: 500; color: {t['text']}; }}
QLabel#hwBadge {{ color: {t['muted']}; }}
QLabel#hint {{ color: {t['muted']}; }}
QLabel#statValue {{ color: {t['muted']}; }}
QLabel#sectionLabel {{ color: {t['muted']}; background: {t['panel']};
    padding: 4px 10px; border-top: 1px solid {t['border']};
    border-bottom: 1px solid {t['border']}; }}
QPushButton {{ background: {t['btn']}; border: 1px solid {t['border2']};
    border-radius: 6px; padding: 5px 12px; }}
QPushButton:hover {{ border-color: {t['accent']}; }}
QPushButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; }}
QPushButton#primary {{ background: {t['accent']}; color: #fff; font-weight: 500;
    border: none; padding: 6px 16px; }}
QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
QPushButton#primary:disabled {{ background: {t['border2']}; color: {t['muted']}; }}
QPushButton#stopBtn {{ border-color: #7a3a3a; color: {C_ERR}; }}
QPushButton#themeBtn {{ padding: 5px 10px; }}
QFrame#toolSep {{ border-left: 1px solid {t['border2']}; }}
QToolButton {{ background: transparent; border: none; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t['input_bg']}; border: 1px solid {t['border2']};
    border-radius: 6px; padding: 4px 8px; selection-background-color: {t['accent']}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {t['panel']};
    border: 1px solid {t['border2']}; selection-background-color: {t['sel']}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: {t['btn']}; width: 16px; border: none; }}
QTableView {{ background: {t['panel']}; border: none; gridline-color: {t['border']};
    selection-background-color: {t['sel']}; selection-color: {t['text']};
    alternate-background-color: {t['panel2']}; }}
QWidget#mergeFlow {{ background: {t['panel']}; }}
QScrollArea#flowScroll {{ background: {t['panel']}; border: none; }}
QTableView::item {{ padding: 3px 6px; border: none; }}
QHeaderView::section {{ background: {t['panel']}; color: {t['muted']};
    border: none; border-bottom: 1px solid {t['border']}; padding: 5px 6px; }}
QTextEdit#logView {{ background: {t['panel2']}; border: none; }}
QProgressBar {{ background: {t['input_bg']}; border: none; border-radius: 3px;
    height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: #378ADD; border-radius: 3px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {t['border']}; }}
QTabBar::tab {{ background: {t['tab_bg']}; color: {t['muted']};
    padding: 7px 14px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {t['text']};
    border-bottom: 2px solid #378ADD; }}
QSplitter::handle {{ background: {t['bg']}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['handle']}; border-radius: 5px;
    min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t['handle']}; border-radius: 5px;
    min-width: 30px; }}
QToolTip {{ background: {t['panel']}; color: {t['text']};
    border: 1px solid {t['border2']}; padding: 4px 8px; }}
QMessageBox {{ background: {t['panel']}; }}
QMenu {{ background: {t['panel']}; border: 1px solid {t['border2']}; }}
QMenu::item {{ padding: 5px 24px; }}
QMenu::item:selected {{ background: {t['sel']}; }}
QStatusBar {{ background: {t['panel']}; border-top: 1px solid {t['border']}; }}
"""


DARK_QSS = _build(_DARK)
LIGHT_QSS = _build(_LIGHT)


def qss_for(theme: str) -> str:
    """theme: 'dark' | 'light' → QSS 字符串。"""
    return DARK_QSS if theme == "dark" else LIGHT_QSS
