# -*- coding: utf-8 -*-
import random
import math

def safe_num(val, default=0):
    try:
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            import re
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            return float(m[0]) if m else float(default)
        return float(default)
    except: return float(default)

def test_random_logic():
    print("Testing Randomization Logic...")
    
    # Mock shell from Gemini
    shell = {
        "width": "random",
        "length": "random",
        "floor_overrides": {
            "1": { "width": 30000, "length": 50000 }
        }
    }
    
    w_default = 10000
    l_default = 15000
    
    # Logic extracted from updated revit_workers.py
    raw_w = shell.get("width", w_default)
    raw_l = shell.get("length", l_default)
    
    base_w = safe_num(raw_w, 30000) if raw_w != "random" else 30000
    base_l = safe_num(raw_l, 50000) if raw_l != "random" else 50000
    
    print(f"Base Width: {base_w}, Base Length: {base_l}")
    assert base_w == 30000
    assert base_l == 50000
    
    floor_overrides = shell.get("floor_overrides", {})
    levels_total = 15
    floor_dims = []
    
    for k in range(levels_total):
        floor_num = str(k + 1)
        overrides = floor_overrides.get(floor_num, {})
        
        w_val = overrides.get("width", shell.get("width", w_default))
        l_val = overrides.get("length", shell.get("length", l_default))
        
        if w_val == "random":
            w_mm = base_w * random.uniform(0.8, 1.2)
        else:
            w_mm = safe_num(w_val, base_w)
            
        if l_val == "random":
            l_mm = base_l * random.uniform(0.8, 1.2)
        else:
            l_mm = safe_num(l_val, base_l)
            
        w_mm = max(1000.0, w_mm)
        l_mm = max(1000.0, l_mm)
        floor_dims.append((w_mm, l_mm))
    
    print(f"Floor 1 Dims: {floor_dims[0]}")
    assert floor_dims[0] == (30000.0, 50000.0)
    
    print(f"Floor 2 Dims: {floor_dims[1]}")
    assert 24000.0 <= floor_dims[1][0] <= 36000.0
    assert 40000.0 <= floor_dims[1][1] <= 60000.0
    
    print("Randomization Logic PASSED!")

def test_json_extraction():
    print("\nTesting JSON Extraction...")
    
    class MockDispatcher:
        def _extract_json(self, text):
            if "```json" in text:
                return text.split("```json")[1].split("```")[0].strip()
            data = text.strip()
            # Robustness fallbacks
            if data.startswith("orchestrate_build(") and data.endswith(")"):
                data = data[len("orchestrate_build("):-1].strip()
            try:
                start = data.find("{")
                end = data.rfind("}")
                if start != -1 and end != -1:
                    return data[start:end+1].strip()
            except: pass
            return data.strip()

    dispatcher = MockDispatcher()
    
    # Test case 1: Standard markdown
    t1 = "Here is the JSON: ```json\n{\"test\": 1}\n```"
    assert dispatcher._extract_json(t1) == '{"test": 1}'
    
    # Test case 2: Function call wrapper
    t2 = "orchestrate_build({\"test\": 2})"
    assert dispatcher._extract_json(t2) == '{"test": 2}'
    
    # Test case 3: Text with JSON inside
    t3 = "I have generated the plan. {\"test\": 3} Hope you like it!"
    assert dispatcher._extract_json(t3) == '{"test": 3}'
    
    print("JSON Extraction Logic PASSED!")

if __name__ == "__main__":
    test_random_logic()
    test_json_extraction()
