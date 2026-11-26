PROJECT_NAME = "陶瓷色阶复烧分流"
ROLE = "陶瓷厂烧成检验员"
MODEL_LABEL = "CART 回归树"
TARGET = "continuous"
UNIT = "ΔE"
ACTION_LABEL = "签出复烧试片"
RESERVE_LABEL = "炉前试片装窑位"
DEFAULT_DAY = 144
DEFAULT_BUDGET = 20
REPORT_LAG = 1
GROUPS = ("白釉墙砖", "浅釉地砖", "哑釉门砖", "墨釉装饰")
SIGNAL_NAMES = ("soak_deviation", "water_glaze", "quench", "loading")
WATCH_FEATURE = "soak_deviation"
METRIC_LABEL = "平均绝对误差"
FIELD_LABELS = {
    "soak_temp": "保温温度 ℃",
    "glaze_sg": "釉浆比重",
    "cool_rate": "冷却速率 ℃/min",
    "moisture": "坯体湿度 %",
    "refires": "已复烧次数",
    "sheet_area": "装窑面积 m²",
    "approved": "是否开放复烧",
}
FIELD_BOUNDS = {
    "soak_temp": (990, 1240),
    "glaze_sg": (1.1, 2.0),
    "cool_rate": (1, 18),
    "moisture": (0.4, 11),
    "refires": (0, 3),
    "sheet_area": (4, 70),
    "approved": (0, 1),
}
CRITICAL_FIELDS = frozenset(("approved", "sheet_area", "soak_temp"))
