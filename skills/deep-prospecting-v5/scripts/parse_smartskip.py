#!/usr/bin/env python3
"""Stage 0 - SmartSkip parser. Reads EITHER SmartSkip export format and normalizes
to one internal record structure so everything downstream is format-blind.

Format 1 (vertical): one row per person, grouped by "Input Name"; each row tagged
  Subject / Relative / Associate with a Possible Type. Up to 18 phones per person.
Format 2 (wide): one row per property; RELATIVE 1..50: / ASSOCIATE 1..5: column groups.
  Relatives capped at 5 phones each by SmartSkip.

Rules baked in:
  - keep Subject + Relatives, DROP Associates
  - drop absurd ages (>100) - bad data
  - relationship type -> canonical tag relative to the SUBJECT, gender from first name
    (Child->Son/Daughter, Parent->Mother/Father, Sibling->Brother/Sister, Spouse->Husband/Wife,
     In-law->In-Law, Other/Unknown->Relative). Ambiguous gender -> neutral (Child/Parent/Sibling/Spouse).
  - has_results = any phone on subject or any kept relative
"""
import csv, re, sys
try:
    import gender_guesser.detector as _gg
    _DET=_gg.Detector(case_sensitive=False)
except Exception:
    _DET=None

def digits(s):
    d=re.sub(r"\D","",s or "")
    if len(d)==11 and d.startswith("1"): d=d[1:]
    return d if len(d)==10 else None

def nkey(first,last):
    toks=[t for t in re.split(r"[ .]+", ("%s %s"%(first or "",last or "")).upper()) if len(t)>1]
    return (toks[0],toks[-1]) if len(toks)>=2 else None

def gender(first):
    if not _DET or not first: return "?"
    g=_DET.get_gender(first.strip().split()[0])
    if g in ("male","mostly_male"): return "M"
    if g in ("female","mostly_female"): return "F"
    return "?"

def absurd(age):
    try: return int(str(age).strip())>100
    except: return False

# relationship type (SmartSkip Possible Type) -> canonical tag, given gender
def canon_rel(ptype, first):
    t=(ptype or "").strip().lower()
    g=gender(first)
    if t=="child":   return {"M":"Son","F":"Daughter"}.get(g,"Child")
    if t=="parent":  return {"M":"Father","F":"Mother"}.get(g,"Parent")
    if t=="sibling": return {"M":"Brother","F":"Sister"}.get(g,"Sibling")
    if t=="spouse":  return {"M":"Husband","F":"Wife"}.get(g,"Spouse")
    if t in ("in-law","in law","inlaw"): return "In-Law"
    # "Other Relative", "Unknown", blank -> generic
    return "Relative"

# canonical tag -> message-board group header (plural, "of <subject>")
GROUP={"Son":"Sons","Daughter":"Daughters","Child":"Children",
       "Father":"Parents","Mother":"Parents","Parent":"Parents",
       "Brother":"Siblings","Sister":"Siblings","Sibling":"Siblings",
       "Husband":"Spouse","Wife":"Spouse","Spouse":"Spouse",
       "In-Law":"In-Laws","Relative":"Other Relatives"}

def detect_format(header):
    if "Input Name" in header: return 1
    if any(h.startswith("RELATIVE 1: First Name") for h in header): return 2
    raise SystemExit("Unrecognized SmartSkip format (no 'Input Name' and no 'RELATIVE 1:' columns)")

def _person(first,last,ptype,age,ms,mc,mstate,mzip,phones,deceased=False):
    nm=("%s %s"%(first or "",last or "")).strip()
    return {"first":(first or "").strip(),"last":(last or "").strip(),"name":nm,
            "type_raw":(ptype or "").strip(),"canon_rel":canon_rel(ptype,first),
            "gender":gender(first),"age":str(age or "").strip(),
            "mailing_street":(ms or "").strip(),"mailing_city":(mc or "").strip(),
            "mailing_state":(mstate or "").strip(),"mailing_zip":(mzip or "").strip(),
            "phones":phones,"deceased":deceased}

def _dedupe(phones):
    """collapse exact-duplicate numbers within one person (SmartSkip sometimes repeats)."""
    seen=set(); out=[]
    for p in phones:
        if p["number"] in seen: continue
        seen.add(p["number"]); out.append(p)
    return out

def _phones_flat(row, prefix, n):
    out=[]
    for i in range(1,n+1):
        num=digits(row.get("%sPhone %d number"%(prefix,i),""))
        if num: out.append({"number":num,"type":(row.get("%sPhone %d type"%(prefix,i),"") or "").strip()})
    return _dedupe(out)

def parse(path):
    rows=list(csv.DictReader(open(path,newline="",encoding="utf-8-sig")))
    if not rows: return []
    fmt=detect_format(rows[0].keys())
    recs=[]
    if fmt==1:
        from collections import OrderedDict
        groups=OrderedDict()
        for r in rows: groups.setdefault(r["Input Name"],[]).append(r)
        for inp,grp in groups.items():
            subj=next((x for x in grp if x["Relationship"]=="Subject"), grp[0])
            rec={"input_name":inp,"first":subj["First Name"].strip(),"last":subj["Last Name"].strip(),
                 "property_address":subj.get("Property Address","").strip(),"property_city":subj.get("Property City","").strip(),
                 "property_state":subj.get("Property State","").strip(),"property_zip":subj.get("Property Zip","").strip(),
                 "mailing_address":subj.get("Mailing Address","").strip(),"mailing_city":subj.get("Mailing City","").strip(),
                 "mailing_state":subj.get("Mailing State","").strip(),"mailing_zip":subj.get("Mailing Zip","").strip(),
                 "deceased":str(subj.get("Deceased","")).strip().lower() in ("true","y","yes","1"),
                 "subject_phones":_phones_flat(subj,"",18),"relatives":[]}
            akeys=[]
            for x in grp:
                if x["Relationship"]=="Associate":
                    k=nkey(x["First Name"],x["Last Name"])
                    if k: akeys.append(list(k))
                    continue
                if x["Relationship"]!="Relative": continue
                if absurd(x.get("Age","")): continue
                rec["relatives"].append(_person(x["First Name"],x["Last Name"],x.get("Possible Type"),x.get("Age"),
                    x.get("Mailing Address"),x.get("Mailing City"),x.get("Mailing State"),x.get("Mailing Zip"),
                    _phones_flat(x,"",18), str(x.get("Deceased","")).strip().lower() in ("true","y","yes","1")))
            rec["associate_keys"]=akeys
            recs.append(rec)
    else:
        for r in rows:
            rec={"input_name":("%s %s"%(r["First Name"],r["Last Name"])).strip(),
                 "first":r["First Name"].strip(),"last":r["Last Name"].strip(),
                 "property_address":r.get("Property Address","").strip(),"property_city":r.get("Property City","").strip(),
                 "property_state":r.get("Property State","").strip(),"property_zip":r.get("Property Zip","").strip(),
                 "mailing_address":r.get("Mailing Address","").strip(),"mailing_city":r.get("Mailing City","").strip(),
                 "mailing_state":r.get("Mailing State","").strip(),"mailing_zip":r.get("Mailing Zip","").strip(),
                 "deceased":str(r.get("Deceased","")).strip().lower() in ("true","y","yes","1"),
                 "subject_phones":_phones_flat(r,"",18),"relatives":[]}
            for i in range(1,51):
                fn=r.get("RELATIVE %d: First Name"%i,"")
                if not (fn and fn.strip()): continue
                if absurd(r.get("RELATIVE %d: Age"%i,"")): continue
                ph=[]
                for j in range(1,6):
                    num=digits(r.get("RELATIVE %d: Phone %d number"%(i,j),""))
                    if num: ph.append({"number":num,"type":(r.get("RELATIVE %d: Phone %d type"%(i,j),"") or "").strip()})
                rec["relatives"].append(_person(fn,r.get("RELATIVE %d: Last Name"%i),r.get("RELATIVE %d: Possible Type"%i),
                    r.get("RELATIVE %d: Age"%i),r.get("RELATIVE %d: Mailing Street"%i),r.get("RELATIVE %d: Mailing City"%i),
                    r.get("RELATIVE %d: Mailing State"%i),r.get("RELATIVE %d: Mailing ZIP Code"%i),_dedupe(ph)))
            akeys=[]
            for i in range(1,6):
                fn=r.get("ASSOCIATE %d: First Name"%i,"")
                if fn and fn.strip():
                    k=nkey(fn,r.get("ASSOCIATE %d: Last Name"%i));
                    if k: akeys.append(list(k))
            rec["associate_keys"]=akeys
            recs.append(rec)
    for rec in recs:
        rec["has_results"]=bool(rec["subject_phones"]) or any(rl["phones"] for rl in rec["relatives"])
        rec["format"]=fmt
    return recs

if __name__=="__main__":
    import json
    recs=parse(sys.argv[1])
    print(json.dumps(recs,indent=2))
