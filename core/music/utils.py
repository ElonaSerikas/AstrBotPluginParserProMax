from enum import IntFlag


class SendMode(IntFlag):
    CARD = 1
    RECORD = 2
    FILE = 4
    IMAGE = 8
    TEXT = 16


def parse_user_input(arg: str):
    """Parse user input for song selection: '2 语音' → (2, [SendMode.RECORD], None)"""
    parts = arg.strip().split()
    if not parts:
        return 0, [], "请输入数字"

    index = 0
    modes = []

    for part in parts:
        if part.isdigit():
            index = int(part)
        elif "语音" in part or "record" in part:
            modes.append(SendMode.RECORD)
        elif "文件" in part or "file" in part:
            modes.append(SendMode.FILE)
        elif "卡片" in part or "card" in part:
            modes.append(SendMode.CARD)
        elif "图片" in part or "image" in part:
            modes.append(SendMode.IMAGE)
        elif "文本" in part or "text" in part:
            modes.append(SendMode.TEXT)

    if index <= 0:
        return 0, [], "请输入有效序号"

    return index, modes, None
