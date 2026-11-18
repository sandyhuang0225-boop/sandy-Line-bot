import re

files = [
    'nhi_demo.html',
    'voice.html',
    'nhi/index.html',
    'pages/kisqali_nhi_demo.html',
    'pages/nhi_v2.html',
    'pages/voice.html'
]

for filepath in files:
    with open(filepath, 'r', encoding='utf-8') as f:
        code = f.read()

    # 1. Support Chinese numerals in nodes extraction
    old_nodes_block = """            // Extract Nodes, Size, Grade robustly
            let nodes = null;
            if (/無淋巴|0\\s*顆|零顆|未轉移|n0|沒有淋巴|淋巴結無/i.test(raw)) {
                nodes = 0;
            } else {
                const nm1 = raw.match(/(?:陽性)?淋巴(?:結|腺)?[^\\d]*?(\\d+)\\s*(?:顆|個|公分|cm)?/i);
                const nm2 = raw.match(/(\\d+)\\s*(?:顆|個)(?:陽性|淋巴)?/i);
                const nm3 = raw.match(/p?n([0-3])/i);
                if (nm1) {
                    nodes = parseInt(nm1[1]);
                } else if (nm2) {
                    nodes = parseInt(nm2[1]);
                } else if (nm3) {
                    nodes = {'0': 0, '1': 2, '2': 5, '3': 10}[nm3[1]];
                }
            }"""

    new_nodes_block = """            // Extract Nodes, Size, Grade robustly (Supports Chinese numerals: 兩顆, 二顆, 三顆, 0顆)
            let nodes = null;
            if (/無淋巴|0\\s*顆|零顆|未轉移|n0|沒有淋巴|淋巴結無/i.test(raw)) {
                nodes = 0;
            } else {
                const cnNumMap = {'零': 0, '一': 1, '二': 2, '兩': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10};
                const nm1 = raw.match(/(?:陽性)?淋巴(?:結|腺)?[^\\d零一二兩两三四五六七八九十]*?(\\d+|[零一二兩两三四五六七八九十]+)\\s*(?:顆|個)?/i);
                const nm2 = raw.match(/(\\d+|[零一二兩两三四五六七八九十]+)\\s*(?:顆|個)(?:陽性|淋巴)?/i);
                const nm3 = raw.match(/p?n([0-3])/i);
                if (nm1) {
                    nodes = cnNumMap[nm1[1]] !== undefined ? cnNumMap[nm1[1]] : parseInt(nm1[1]);
                } else if (nm2) {
                    nodes = cnNumMap[nm2[1]] !== undefined ? cnNumMap[nm2[1]] : parseInt(nm2[1]);
                } else if (nm3) {
                    nodes = {'0': 0, '1': 2, '2': 5, '3': 10}[nm3[1]];
                }
            }"""

    if old_nodes_block in code:
        code = code.replace(old_nodes_block, new_nodes_block)
        print(f"Patched nodes block in {filepath}")
    else:
        print(f"Could not find old_nodes_block in {filepath}")

    # 2. Update condition II threshold from size > 2.0 to size >= 2.0 across evaluateClinicalQuery, recalculateFromInputs, and syncApplicationFormReason
    code = re.sub(r'nodes\s*>=\s*1\s*&&\s*nodes\s*<=\s*3\s*&&\s*size\s*>\s*2\.0', 'nodes >= 1 && nodes <= 3 && size >= 2.0', code)
    code = re.sub(r'nodesVal\s*>=\s*1\s*&&\s*nodesVal\s*<=\s*3\s*&&\s*sizeVal\s*>\s*2\.0', 'nodesVal >= 1 && nodesVal <= 3 && sizeVal >= 2.0', code)
    code = re.sub(r'T2\s*>\s*2cm', 'T2 ≧ 2cm', code)

    # 3. Update ineligible explanation check:
    # If sizeVal < 2.0 (T0N1, T1N1 with size strictly < 2.0cm), G1/2 is ineligible. But 2.0cm is T2 (eligible under Condition II)!
    code = re.sub(r'sizeVal\s*<=\s*2\.0\s*&&\s*gradeVal\s*<\s*3', 'sizeVal < 2.0 && gradeVal < 3', code)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(code)

print("Batch update complete!")
