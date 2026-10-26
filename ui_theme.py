"""原生主题的自定义区域配色；不维护第二套外观选择状态。"""

DARK = {
    "bg":"#0d1420", "surface":"#151e2b", "surface2":"#1c293b", "hover":"#2a3b50",
    "ink":"#edf2f8", "body":"#d2dce9", "muted":"#b1bfd1", "line":"#42536a",
    "gold":"#e4c38d", "accent-fill":"#e4c38d", "on-accent":"#17202a",
    "stage1":"#273749", "stage2":"#1b2939", "stage3":"#152131", "stage4":"#111b29",
    "avatar":"#263345", "wolf-bg":"#3a2b35", "wolf":"#f2b9c3",
    "god-bg":"#253c43", "god":"#b4dfdf", "positive":"#a5dfc5",
    "thinking-bg":"#453d2e", "thinking":"#f6dba5", "on-bar":"#ffffff",
}

LIGHT = {
    "bg":"#f2f5f9", "surface":"#ffffff", "surface2":"#e8eef6", "hover":"#dce6f2",
    "ink":"#1c293b", "body":"#2e4057", "muted":"#4c5e75", "line":"#b7c4d4",
    "gold":"#76501b", "accent-fill":"#79521c", "on-accent":"#ffffff",
    "stage1":"#dfe9f4", "stage2":"#eaf1f8", "stage3":"#f6f9fc", "stage4":"#e9f0f7",
    "avatar":"#e0e9f4", "wolf-bg":"#f7e5eb", "wolf":"#8e3048",
    "god-bg":"#dff0ef", "god":"#155e62", "positive":"#155e44",
    "thinking-bg":"#f8e9c9", "thinking":"#6b4514", "on-bar":"#ffffff",
}


def theme_variables(fallback="light"):
    """自定义区域跟随 stApp 的原生 color-scheme，即时响应菜单切换。

    light-dark() 在浏览器中解析，不依赖 st.context 的下一次 Python rerun。
    老浏览器使用当前原生主题的调色板兜底；不改写原生 CSS 变量或 color-scheme。
    """
    palette = DARK if fallback == "dark" else LIGHT
    fixed = ";".join(f"--{key}:{value}" for key, value in palette.items())
    live = ";".join(f"--{key}:light-dark({LIGHT[key]},{DARK[key]})" for key in LIGHT)
    return (f".stApp{{{fixed}}}" + "@supports(color:light-dark(white,black)){"
            + f".stApp{{{live}}}" + "}")


def contrast_ratio(foreground, background):
    """sRGB 相对亮度；用于测试正文和真实按钮配色，不宣称整个站点已无障碍认证。"""
    def luminance(color):
        channels = [int(color[i:i+2],16)/255 for i in (1,3,5)]
        linear = [x/12.92 if x <= 0.04045 else ((x+0.055)/1.055)**2.4 for x in channels]
        return sum(a*b for a,b in zip(linear, (0.2126,0.7152,0.0722)))
    first, second = sorted([luminance(foreground), luminance(background)])
    return (second+0.05)/(first+0.05)
