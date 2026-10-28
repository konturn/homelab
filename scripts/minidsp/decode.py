import xml.etree.ElementTree as ET, sys, json
def decode(path):
    r=ET.parse(path).getroot()
    it={i.get("name"):i.find("dec").text for i in r.findall("item")}
    fl={f.get("name"):{c.tag:(c.text or "").strip() for c in f} for f in r.findall("filter")}
    on=lambda k: it.get(k)=="2"
    out={}
    out["master_mute"]=r.findtext("master_mute")
    out["inputs"]={f"in{n}":{"gain_db":float(it[f"DGain_{n}_0"]),"muted":not on(f"DGain_{n}_0_status"),"polarity_inv":it[f"polarity_in_{n}_0"]=="1",
        "peq":[ {"band":b,"type":fl[f"PEQ_{n}_{b}"]["type"],"freq":float(fl[f"PEQ_{n}_{b}"]["freq"]),"gain":float(fl[f"PEQ_{n}_{b}"]["boost"]),"q":float(fl[f"PEQ_{n}_{b}"]["q"])}
                for b in range(1,11) if fl[f"PEQ_{n}_{b}"]["bypass"]=="0" and float(fl[f"PEQ_{n}_{b}"]["boost"])!=0]} for n in (1,2)}
    outs={}
    for o in range(1,5):
        d=o+2
        routes=[f"in{i+1}({float(it[f'MixerNxMSmoothed1_{i}_{o-1}']):+g}dB)" for i in (0,1) if on(f"MixerNxMSmoothed1_{i}_{o-1}_status")]
        xo=[]
        for k,lab in ((1,"LPF"),(5,"HPF")):
            f=fl[f"BPF_{d}_{k}"]
            if f["bypass"]=="0": xo.append(f'{f["type"]}@{float(f["freq"]):g}Hz')
        peq=[f'{fl[f"PEQ_{d}_{b}"]["type"]} {float(fl[f"PEQ_{d}_{b}"]["freq"]):g}Hz {float(fl[f"PEQ_{d}_{b}"]["boost"]):+g}dB Q{float(fl[f"PEQ_{d}_{b}"]["q"]):g}'
             for b in range(1,11) if fl[f"PEQ_{d}_{b}"]["bypass"]=="0" and (float(fl[f"PEQ_{d}_{b}"]["boost"])!=0 or fl[f"PEQ_{d}_{b}"]["type"] not in ("PK",))]
        outs[f"out{o}"]={"gain_db":float(it[f"DGain_{d}_0"]),"muted":not on(f"DGain_{d}_0_status"),"routing":routes,"delay_ms":float(it[f"Delay_{d}_0"]) if it.get(f"Delay_{d}_0_status")=="2" else 0.0,
            "delay_raw":it[f"Delay_{d}_0"]+("" if it.get(f"Delay_{d}_0_status")=="2" else " (status "+it.get(f"Delay_{d}_0_status","?")+")"),
            "polarity_inv":it[f"polarity_out_{o}_0"]=="1","crossover":xo,"peq":peq,
            "limiter":(f'thr {it[f"COMP_{d}_0_threshold"]}dB ratio {it[f"COMP_{d}_0_ratio"]}' if it[f"COMP_{d}_0_status"]=="2" else "off"),
            "fir":"on" if it.get(f"FIR_{d}_0_status")=="2" else "off"}
    out["outputs"]=outs
    out["link"]=r.findtext("link")
    return out
if __name__=="__main__":
    for p in sys.argv[1:]:
        d=decode(p); print("=====",p)
        print(" master_mute",d["master_mute"],"| link",d["link"])
        for k,v in d["inputs"].items(): print(" ",k,v)
        for k,v in d["outputs"].items(): print(" ",k, json.dumps(v))
