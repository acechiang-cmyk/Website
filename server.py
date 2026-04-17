import os, json, csv, io, re
from datetime import datetime
from typing import Optional
import anthropic
from communications import send_sms
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db, engine
from models import Base, Agency, User, Lead, Message, Contact
from auth import (
    hash_password, verify_password, create_token, decode_token,
    require_auth, require_admin, require_owner
)

Base.metadata.create_all(bind=engine)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

SYSTEM_PROMPT = """You are Aria, a friendly and knowledgeable assistant for {agency_name}.

Your goals:
1. Warmly greet visitors and learn about their property management needs
2. Answer questions about services, pricing, and coverage areas
3. Qualify leads by naturally collecting: name, email, phone number, and property details
4. Encourage interested visitors to schedule a free consultation

Tone: Professional yet warm. Never pushy. Do not use emojis. Keep responses short — 1 to 2 sentences maximum. Ask one question at a time.

When you have collected a lead's name AND either email or phone, include this at the very end of your message:
<!--LEAD:{{"name":"...","email":"...","phone":"...","notes":"..."}}-->"""

app = FastAPI(title="Nelson CRM")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

# ── Auth ──────────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    agency_name: str
    name: str
    email: str
    password: str

@app.post("/api/auth/signup")
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(400, "Email already registered")
    slug = re.sub(r"[^a-z0-9]+", "-", req.agency_name.lower()).strip("-")
    base_slug = slug; count = 1
    while db.query(Agency).filter(Agency.slug == slug).first():
        slug = f"{base_slug}-{count}"; count += 1
    agency = Agency(name=req.agency_name, slug=slug)
    db.add(agency); db.flush()
    user = User(agency_id=agency.id, email=req.email, name=req.name,
                password_hash=hash_password(req.password), role="owner")
    db.add(user); db.commit(); db.refresh(user)
    token = create_token(user.id, agency.id, user.role)
    return {"token": token, "agency": {"id": agency.id, "name": agency.name, "slug": slug},
            "user": {"id": user.id, "name": user.name, "role": user.role}}

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email, User.is_active == True).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    agency = db.query(Agency).filter(Agency.id == user.agency_id).first()
    token = create_token(user.id, agency.id, user.role)
    response.set_cookie("token", token, httponly=True, samesite="lax", max_age=60*60*24*30)
    return {"token": token, "agency": {"id": agency.id, "name": agency.name, "slug": agency.slug},
            "user": {"id": user.id, "name": user.name, "role": user.role}}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("token")
    return {"ok": True}

@app.get("/api/auth/me")
def me(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    agency = db.query(Agency).filter(Agency.id == user.agency_id).first()
    return {"user": {"id": user.id, "name": user.name, "role": user.role, "email": user.email},
            "agency": {"id": agency.id, "name": agency.name, "slug": agency.slug}}

# ── Team ──────────────────────────────────────────────────────────────────────
class InviteRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "agent"

@app.post("/api/team/invite")
def invite(req: InviteRequest, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(400, "Email already registered")
    if req.role not in ("agent", "admin"):
        raise HTTPException(400, "Invalid role")
    new_user = User(agency_id=user.agency_id, email=req.email, name=req.name,
                    password_hash=hash_password(req.password), role=req.role)
    db.add(new_user); db.commit(); db.refresh(new_user)
    return {"id": new_user.id, "name": new_user.name, "email": new_user.email, "role": new_user.role}

@app.get("/api/team")
def get_team(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    members = db.query(User).filter(User.agency_id == user.agency_id, User.is_active == True).all()
    return [{"id": m.id, "name": m.name, "email": m.email, "role": m.role, "created_at": str(m.created_at)} for m in members]

# ── Public agency lookup (for chatbot widget) ─────────────────────────────────
@app.get("/api/agency/{slug}")
def get_agency_by_slug(slug: str, db: Session = Depends(get_db)):
    agency = db.query(Agency).filter(Agency.slug == slug).first()
    if not agency: raise HTTPException(404)
    return {"id": agency.id, "name": agency.name, "slug": agency.slug}

# ── Round-robin assignment ────────────────────────────────────────────────────
def _round_robin_agent(agency_id: int, db: Session) -> Optional[int]:
    agents = db.query(User).filter(User.agency_id == agency_id, User.is_active == True).all()
    if not agents:
        return None
    # Pick agent with fewest assigned leads
    counts = {a.id: 0 for a in agents}
    for row in db.query(Lead.assigned_to).filter(
        Lead.agency_id == agency_id, Lead.assigned_to != None
    ).all():
        if row[0] in counts:
            counts[row[0]] += 1
    return min(counts, key=counts.get)

# ── Chat ──────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str
    agency_id: int

@app.post("/api/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    if not ai_client:
        raise HTTPException(503, "AI not configured — set ANTHROPIC_API_KEY")
    agency = db.query(Agency).filter(Agency.id == req.agency_id).first()
    if not agency:
        raise HTTPException(404, "Agency not found")

    lead = db.query(Lead).filter(Lead.session_id == req.session_id, Lead.agency_id == req.agency_id).first()
    if not lead:
        assigned = _round_robin_agent(req.agency_id, db)
        lead = Lead(session_id=req.session_id, agency_id=req.agency_id,
                    source="chatbot", assigned_to=assigned)
        db.add(lead); db.commit(); db.refresh(lead)

    db.add(Message(lead_id=lead.id, agency_id=req.agency_id, session_id=req.session_id, role="user", content=req.message))
    db.commit()

    history = [{"role": m.role, "content": m.content}
               for m in db.query(Message).filter(Message.session_id == req.session_id).order_by(Message.id).all()]
    system_prompt = SYSTEM_PROMPT.format(agency_name=agency.name)

    # Capture primitive values — SQLAlchemy objects detach after session closes
    lead_id    = lead.id
    agency_id  = req.agency_id
    session_id = req.session_id
    agency_name = agency.name

    def generate():
        full_response = ""
        try:
            with ai_client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system_prompt,
                messages=history
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    # Only stream text before the lead marker
                    if "<!--LEAD:" in full_response:
                        before = full_response.split("<!--LEAD:")[0]
                        already_sent = full_response[:full_response.index("<!--LEAD:")-len(text)]
                        new_visible = before[len(already_sent):]
                        if new_visible:
                            yield f"data: {json.dumps({'text': new_visible})}\n\n"
                    else:
                        yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': ' Sorry, something went wrong. Please try again.'})}\n\n"

        # Post-stream: use a fresh session — original is detached by now
        try:
            from database import SessionLocal
            fresh_db = SessionLocal()
            visible = full_response.split("<!--LEAD:")[0].strip() if "<!--LEAD:" in full_response else full_response

            if "<!--LEAD:" in full_response:
                lead_json = full_response.split("<!--LEAD:")[1].split("-->")[0]
                data = json.loads(lead_json)
                fresh_lead = fresh_db.query(Lead).filter(Lead.id == lead_id).first()
                if fresh_lead:
                    is_new_phone = not fresh_lead.phone and data.get("phone")
                    if data.get("name"):  fresh_lead.name  = data["name"]
                    if data.get("email"): fresh_lead.email = data["email"]
                    if data.get("phone"): fresh_lead.phone = data["phone"]
                    if data.get("notes"): fresh_lead.notes = data["notes"]
                    fresh_db.commit()
                    if is_new_phone:
                        sms_msg = (f"Hi {fresh_lead.name or 'there'}! Thanks for reaching out to {agency_name}. "
                                   f"We'll be in touch shortly. Reply STOP to opt out.")
                        send_sms(fresh_lead.phone, sms_msg)

            fresh_db.add(Message(lead_id=lead_id, agency_id=agency_id,
                                 session_id=session_id, role="assistant", content=visible))
            fresh_db.commit()
            fresh_db.close()
        except Exception:
            pass

        yield f"data: {json.dumps({'done': True, 'lead_id': lead_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Leads ─────────────────────────────────────────────────────────────────────
@app.get("/api/leads")
def get_leads(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    leads = db.query(Lead).filter(Lead.agency_id == user.agency_id).order_by(Lead.created_at.desc()).all()
    return [_lead_dict(l) for l in leads]

@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int, user: User = Depends(require_auth), db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.agency_id == user.agency_id).first()
    if not lead: raise HTTPException(404)
    msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.id).all()
    d = _lead_dict(lead)
    d["messages"] = [{"role": m.role, "content": m.content, "created_at": str(m.created_at)} for m in msgs]
    return d

class LeadUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    score: Optional[str] = None
    stage: Optional[str] = None
    assigned_to: Optional[int] = None

@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate, user: User = Depends(require_auth), db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.agency_id == user.agency_id).first()
    if not lead: raise HTTPException(404)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(lead, k, v)
    lead.updated_at = datetime.utcnow()
    db.commit()
    return _lead_dict(lead)

@app.get("/api/leads/export/csv")
def export_leads(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    leads = db.query(Lead).filter(Lead.agency_id == user.agency_id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Email", "Phone", "Score", "Stage", "Source", "Notes", "Created"])
    for l in leads:
        writer.writerow([l.id, l.name, l.email, l.phone, l.score, l.stage, l.source, l.notes, l.created_at])
    return Response(content=output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})

# ── Contacts ──────────────────────────────────────────────────────────────────
class ContactRequest(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    timeline: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None

@app.get("/api/contacts")
def get_contacts(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    contacts = db.query(Contact).filter(Contact.agency_id == user.agency_id).order_by(Contact.created_at.desc()).all()
    return [_contact_dict(c) for c in contacts]

@app.post("/api/contacts")
def create_contact(req: ContactRequest, user: User = Depends(require_auth), db: Session = Depends(get_db)):
    c = Contact(agency_id=user.agency_id, **req.model_dump())
    db.add(c); db.commit(); db.refresh(c)
    return _contact_dict(c)

@app.patch("/api/contacts/{contact_id}")
def update_contact(contact_id: int, req: ContactRequest, user: User = Depends(require_auth), db: Session = Depends(get_db)):
    c = db.query(Contact).filter(Contact.id == contact_id, Contact.agency_id == user.agency_id).first()
    if not c: raise HTTPException(404)
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    c.updated_at = datetime.utcnow()
    db.commit()
    return _contact_dict(c)

@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int, user: User = Depends(require_auth), db: Session = Depends(get_db)):
    c = db.query(Contact).filter(Contact.id == contact_id, Contact.agency_id == user.agency_id).first()
    if not c: raise HTTPException(404)
    db.delete(c); db.commit()
    return {"ok": True}

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    aid = user.agency_id
    total   = db.query(Lead).filter(Lead.agency_id == aid).count()
    new     = db.query(Lead).filter(Lead.agency_id == aid, Lead.stage == "new").count()
    showing = db.query(Lead).filter(Lead.agency_id == aid, Lead.stage == "showing").count()
    closed  = db.query(Lead).filter(Lead.agency_id == aid, Lead.stage == "closed").count()
    return {"total": total, "new": new, "showing": showing, "closed": closed}

# ── Helpers ───────────────────────────────────────────────────────────────────
def _lead_dict(l: Lead) -> dict:
    return {"id": l.id, "name": l.name, "email": l.email, "phone": l.phone,
            "notes": l.notes, "score": l.score, "stage": l.stage,
            "source": l.source, "assigned_to": l.assigned_to,
            "created_at": str(l.created_at), "updated_at": str(l.updated_at)}

def _contact_dict(c: Contact) -> dict:
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
            "budget_min": c.budget_min, "budget_max": c.budget_max,
            "timeline": c.timeline, "tags": c.tags, "notes": c.notes,
            "created_at": str(c.created_at)}

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=os.path.dirname(__file__) or ".", html=True), name="static")
