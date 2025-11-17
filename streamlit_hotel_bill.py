#!/usr/bin/env python3
"""
streamlit_hotel_bill.py

Streamlit UI for the hotel bill generator that uses the same logic as your
script: Gemini lookups (if GEMINI_API_KEY in env), fallback addresses/hotels,
font/logo upload, and creates a PDF invoice saved to bytes and offered for download.

Run:
    pip install streamlit reportlab pillow qrcode
    streamlit run streamlit_hotel_bill.py
"""

import os
import io
import json
import random
import re
from datetime import datetime
from textwrap import shorten

import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

# Optional libs
try:
    import qrcode
    from PIL import Image
except Exception:
    qrcode = None
    Image = None

# ---------- Constants ----------
PAGE_MARGIN_MM = 18
DEFAULT_HOTEL_NAME = "NEO ROBOTIC INN"
DEFAULT_GST_PERCENT = 5.0

# ---------- Helpers (same logic as your script) ----------
def rand_gst_number():
    state = f"{random.randint(1,35):02d}"
    pan = ''.join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5)) + \
          ''.join(random.choice("0123456789") for _ in range(4)) + \
          random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return state + pan + str(random.randint(1,9)) + "Z" + random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

def rand_mobile():
    return f"+91-{random.randint(600,999)}{random.randint(1000000,9999999)}"

def money(x, symbol='₹'):
    return f"{symbol}{x:,.2f}"

def get_random_address(city: str) -> str:
    if not city:
        return "12 Circuit Avenue, Tech Park, City"
    c = city.strip().lower()
    city_addresses = {
        "mumbai": [
            "87 Marine Drive, Mumbai",
            "14 Bandra Kurla Complex, Mumbai",
            "5 Colaba Causeway, Mumbai",
            "210 Andheri East, Mumbai"
        ],
        "delhi": [
            "32 Connaught Place, New Delhi",
            "108 Lajpat Nagar, New Delhi",
            "9 Patel Nagar, New Delhi",
            "256 INA Colony, New Delhi"
        ],
        "bangalore": [
            "18 MG Road, Bengaluru",
            "45 Indiranagar, Bengaluru",
            "7 Whitefield Main Road, Bengaluru",
            "88 Koramangala, Bengaluru"
        ],
        "hyderabad": [
            "12 Banjara Hills, Hyderabad",
            "56 Hitech City Rd, Hyderabad",
            "3 Secunderabad Rd, Hyderabad"
        ],
        "chennai": [
            "77 T Nagar, Chennai",
            "21 Anna Salai, Chennai",
            "9 Adyar, Chennai"
        ],
        "kolkata": [
            "15 Park Street, Kolkata",
            "88 Salt Lake, Kolkata",
            "22 Ballygunge, Kolkata"
        ],
        "pune": [
            "11 FC Road, Pune",
            "60 Koregaon Park, Pune",
            "9 Viman Nagar, Pune"
        ],
        "indore": [
            "18 MG Road, Indore",
            "44 Vijay Nagar, Indore",
            "5 AB Road, Indore"
        ],
    }
    if c in city_addresses:
        return random.choice(city_addresses[c])
    for key in city_addresses:
        if c.startswith(key):
            return random.choice(city_addresses[key])
    street_num = random.randint(1, 300)
    street_names = ["Park Lane", "Circuit Avenue", "Industrial Area", "MG Road", "Market Street", "Station Road"]
    street = random.choice(street_names)
    return f"{street_num} {street}, {city.title()}"

# Attempt to fetch a short address using Gemini (if available)
def call_gemini_for_address(city, api_key_env="GEMINI_API_KEY", debug=False):
    api_key = os.environ.get(api_key_env)
    if not api_key:
        if debug:
            st.info("GEMINI_API_KEY not set in environment.")
        return None

    prompt = (
        f"Provide a single plausible street address (one short line) for a hotel in {city}.\n"
        "Return ONLY a short single-line address string or a JSON object like {\"address\": \"...\"}.\n"
        "If you cannot provide JSON, just output the address line."
    )

    raw_text = None
    tried_any = False

    # Try google.genai
    try:
        import google.genai as genai  # type: ignore
        tried_any = True
        genai.configure(api_key=api_key)
        resp = genai.generate(model="gpt-4o-mini-1", prompt=prompt)
        raw_text = getattr(resp, "text", None) or str(resp)
    except Exception as e:
        if debug:
            st.write("google-genai failed:", e)

    # Try google.generativeai
    if raw_text is None:
        try:
            import google.generativeai as genai2  # type: ignore
            tried_any = True
            genai2.configure(api_key=api_key)
            resp = genai2.generate_text(model="gpt-4o-mini-1", prompt=prompt)
            raw_text = getattr(resp, "text", None) or str(resp)
        except Exception as e:
            if debug:
                st.write("google-generativeai failed:", e)

    if not tried_any:
        if debug:
            st.write("No Gemini SDK available in environment.")
        return None

    if not raw_text:
        return None

    # Try JSON extraction
    try:
        m = re.search(r"(\{\s*\"address\"\s*:\s*\".*?\"\s*\})", raw_text, re.S)
        if m:
            js = json.loads(m.group(1))
            addr = js.get("address")
            if addr:
                return addr.strip()
    except Exception:
        if debug:
            st.write("JSON parse for address failed.")

    # Fallback: first plausible line
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[\"'\[\{]+", "", line)
        line = re.sub(r"[\"'\]\}]+$", "", line)
        if re.search(r"\d", line) or any(tok in line.lower() for tok in ["road", "rd", "street", "st", "lane", "drive", "ave", "park", "complex", "colaba"]):
            return line
        if len(line) < 120:
            return line

    return None

# Hotel search helper using Gemini (returns list of hotels with phone)
def call_gemini_hotel_search(city, min_price, max_price, api_key_env="GEMINI_API_KEY", debug=False):
    api_key = os.environ.get(api_key_env)
    if not api_key:
        if debug:
            st.write("call_gemini_hotel_search: GEMINI_API_KEY not set.")
        return None

    prompt = (
        f"List up to 5 hotels in {city} with nightly price around INR {min_price}-{max_price}. "
        "Return a JSON array of objects with fields: name, approx_price, phone. "
        "If you cannot return JSON, output lines like: Hotel Name - INR 3,000 - +91-xxxxx."
    )

    tried_any = False
    raw_text = None

    try:
        import google.genai as genai  # type: ignore
        tried_any = True
        genai.configure(api_key=api_key)
        resp = genai.generate(model="gpt-4o-mini-1", prompt=prompt)
        raw_text = getattr(resp, "text", None) or str(resp)
    except Exception:
        pass

    if raw_text is None:
        try:
            import google.generativeai as genai2  # type: ignore
            tried_any = True
            genai2.configure(api_key=api_key)
            resp = genai2.generate_text(model="gpt-4o-mini-1", prompt=prompt)
            raw_text = getattr(resp, "text", None) or str(resp)
        except Exception:
            pass

    if not tried_any:
        return None

    # extract JSON or parse lines
    try:
        m = re.search(r"(\[\s*\{.*?\}\s*\])", raw_text, re.S)
        if m:
            parsed = json.loads(m.group(1))
            hotels = []
            for h in parsed:
                name = h.get("name", "<unknown>")
                price = float(h.get("approx_price", 0) or h.get("price", 0))
                phone = h.get("phone") or rand_mobile()
                hotels.append({"name": name, "price": price, "phone": phone})
            if hotels:
                return hotels
    except Exception:
        pass

    hotels = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[-–—]", line)
        if len(parts) >= 2:
            name = parts[0].strip()
            price_match = re.search(r"([\d,]+(?:\.\d+)?)", parts[1])
            price = float(price_match.group(1).replace(",", "")) if price_match else 0.0
            phone = None
            phone_match = re.search(r"(\+?\d[\d\-\s]{7,}\d)", line)
            if phone_match:
                phone = phone_match.group(1).strip()
            hotels.append({"name": name, "price": price, "phone": phone or rand_mobile()})
    return hotels[:5] if hotels else None

def fallback_hotel_suggestions(city, bill_amount):
    names = ["Grand Plaza", "Mirage Residency", "Sunset Suites", "City Comfort", "Hotel Aurora", "Royal Stay"]
    out = []
    for i in range(3):
        name = f"{random.choice(names)} {city.split()[0].title()}"
        delta = bill_amount * random.uniform(-0.2, 0.2)
        price = max(500, round(bill_amount + delta, 2))
        phone = rand_mobile()
        out.append({"name": name, "price": price, "phone": phone})
    return out

# Register uploaded TTF to reportlab (returns font name)
def register_font_from_bytes(ttf_bytes, filename_hint="uploaded_font.ttf"):
    path = os.path.join(".", f".tmp_font_{random.randint(1000,9999)}_{filename_hint}")
    with open(path, "wb") as f:
        f.write(ttf_bytes)
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        pdfmetrics.registerFont(TTFont(name, path))
        return name, path
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise

# ---------- PDF generator: writes to bytes ----------
def create_pdf_bytes(hotel_name, hotel_addr, guest_name, invoice_no, date_str, room_no,
                     items, gst_no, gst_percent, payment_mode, hotel_phone,
                     font_name=None, logo_file=None, currency='₹'):
    """
    Generate PDF into bytes and return bytes buffer.
    Width is reduced by 30% (content = 70% of printable width) and centered.
    """
    w, h = A4
    margin = PAGE_MARGIN_MM * mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    # compute reduced centered area (content = 70% of printable width)
    full_printable_width = w - 2 * margin
    content_width = full_printable_width * 0.70   # 70% -> reduced by 30%
    left_offset = margin + (full_printable_width - content_width) / 2
    top_y_origin = h - margin

    # translate to the top-left of the content area
    c.translate(left_offset, top_y_origin)

    header_font = font_name if font_name else "Helvetica-Bold"
    regular_font = font_name if font_name else "Helvetica"

    # logo if provided as binary/file-like
    title_x = 0
    top_y = 0
    if logo_file is not None:
        try:
            # logo_file may be an UploadedFile or bytes
            if hasattr(logo_file, "read"):
                logo_bytes = logo_file.read()
                logo_stream = io.BytesIO(logo_bytes)
            elif isinstance(logo_file, (bytes, bytearray)):
                logo_stream = io.BytesIO(logo_file)
            else:
                logo_stream = logo_file
            img = ImageReader(logo_stream)
            iw, ih = img.getSize()
            logo_w = 40 * mm
            logo_h = (logo_w / iw) * ih
            c.drawImage(img, 0, -logo_h, width=logo_w, height=logo_h, preserveAspectRatio=True, mask='auto')
            title_x = logo_w + 8
            top_y = -logo_h / 2
        except Exception:
            title_x = 0
            top_y = 0

    # header text
    c.setFont(header_font, 18)
    c.drawString(title_x, top_y, hotel_name)
    c.setFont(regular_font, 9)
    c.drawString(title_x, top_y - 16, hotel_addr)
    c.drawString(title_x, top_y - 28, f"Phone: {hotel_phone}")
    c.drawString(title_x, top_y - 40, f"GSTIN: {gst_no}")

    # compute right column baseline relative to content_width
    right_x = content_width - 160
    c.setFont(regular_font, 9)
    c.drawString(right_x, top_y, f"Invoice No: {invoice_no}")
    c.drawString(right_x, top_y - 12, f"Date: {date_str}")

    # separator
    c.line(0, top_y - 56, content_width, top_y - 56)

    # guest block
    y = top_y - 76
    c.setFont(header_font, 10)
    c.drawString(0, y, "Guest Name:")
    c.setFont(regular_font, 10)
    c.drawString(90, y, guest_name)
    c.setFont(header_font, 10)
    c.drawString(right_x, y, "Room No:")
    c.setFont(regular_font, 10)
    c.drawString(right_x + 60, y, str(room_no))

    y -= 18
    c.setFont(header_font, 10)
    c.drawString(0, y, "Items")
    y -= 12
    c.setFont(regular_font, 10)

    # NEW: wider/spaced columns derived from content_width
    col_sl_x = 0
    col_desc_x = 36                              # give more room for description
    col_qty_right = content_width * 0.60         # qty column right edge (60% across)
    col_rate_right = content_width * 0.80        # rate column right edge (80% across)
    col_amount_right = content_width             # amount at the far right

    # header row (use drawRightString for Qty/Rate/Amount headers so they align with data)
    c.drawString(col_sl_x, y, "SL")
    c.drawString(col_desc_x, y, "Description")
    c.drawRightString(col_qty_right, y, "Qty")
    c.drawRightString(col_rate_right, y, "Rate")
    c.drawRightString(col_amount_right, y, "Amount")
    y -= 6
    c.line(0, y, content_width, y)
    y -= 14

    subtotal = 0.0
    desc_max_chars = 50
    for i, it in enumerate(items, start=1):
        qty = int(it.get("qty", 1))
        rate = float(it.get("rate", 0.0))
        amount = qty * rate
        subtotal += amount

        c.setFont(regular_font, 10)
        c.drawString(col_sl_x, y, str(i))
        desc = shorten(str(it.get("desc", "")), width=desc_max_chars, placeholder="...")
        c.drawString(col_desc_x, y, desc)
        c.drawRightString(col_qty_right, y, str(qty))
        c.drawRightString(col_rate_right, y, money(rate, currency))
        c.drawRightString(col_amount_right, y, money(amount, currency))

        y -= 16
        if y < -500:
            c.showPage()
            c.translate(left_offset, top_y_origin)
            y = -40

    # totals
    gst_amount = round(subtotal * gst_percent / 100.0, 2)
    grand_total = round(subtotal + gst_amount, 2)
    y -= 8
    c.line(0, y, content_width, y)
    y -= 16
    c.setFont(header_font, 10)
    c.drawRightString(content_width, y, f"Subtotal: {money(subtotal, currency)}")
    y -= 14
    c.drawRightString(content_width, y, f"GST ({gst_percent}%): {money(gst_amount, currency)}")
    y -= 14
    c.drawRightString(content_width, y, f"Grand Total: {money(grand_total, currency)}")

    y -= 26
    c.setFont(regular_font, 9)
    c.drawString(0, y, f"Payment Mode: {payment_mode}")
    y -= 12
    c.drawString(0, y, "Note: This is a computer-generated bill.")

    c.save()
    buf.seek(0)
    return buf.read()

# ---------- Streamlit UI ----------
st.set_page_config(page_title="Hotel Bill Generator", layout="centered")
st.title("Hotel Bill Generator — Streamlit")

with st.form("bill_form"):
    col1, col2 = st.columns([2, 1])

    with col1:
        hotel_name = st.text_input("Hotel name", value=DEFAULT_HOTEL_NAME)
        city = st.text_input("City (for address lookup)", value="Mumbai")
        hotel_logo = st.file_uploader("Logo (optional PNG/JPG)", type=["png", "jpg", "jpeg"])
        font_file = st.file_uploader("Upload TTF font (optional, e.g. RobotoMono.ttf)", type=["ttf"])
        hotel_phone_input = st.text_input("Hotel phone (optional)", value="")
    with col2:
        guest_name = st.text_input("Customer name", value="Guest")
        room_no = st.text_input("Room no.", value="101")
        invoice_no = st.text_input("Invoice no.", value=f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        date_str = st.text_input("Date", value=datetime.now().strftime("%Y-%m-%d"))
        bill_amount = st.number_input("Bill amount (INR)", value=1000.0, min_value=0.0, step=50.0)

    gst_percent = st.number_input("GST percent", value=DEFAULT_GST_PERCENT, min_value=0.0, step=0.5)
    force_fallback = st.checkbox("Force fallback (skip Gemini calls)", value=False)
    debug = st.checkbox("Debug (show Gemini raw output in app logs)", value=False)

    # Items: keep single line item equal to bill_amount (as before) but allow extra
    st.markdown("**Line item** (by default: Room & Services = bill amount)")
    desc = st.text_input("Item description", value="Room & Services")
    add_custom_items = st.checkbox("Add extra items", value=False)
    items = []
    if add_custom_items:
        st.info("Add custom items: description, qty, rate — one per line in the format `desc,qty,rate`")
        raw_items = st.text_area("Custom items (one per line):", value=f"{desc},1,{bill_amount}")
        for line in raw_items.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    items.append({"desc": parts[0], "qty": int(parts[1]), "rate": float(parts[2])})
                except Exception:
                    continue
    else:
        items = [{"desc": desc, "qty": 1, "rate": float(bill_amount)}]

    submitted = st.form_submit_button("Generate bill (PDF)")

if submitted:
    # register font if uploaded
    font_name = None
    font_tmp_path = None
    if font_file is not None:
        try:
            font_bytes = font_file.read()
            font_name, font_tmp_path = register_font_from_bytes(font_bytes, font_file.name)
            st.success(f"Registered font: {font_name}")
        except Exception as e:
            st.error(f"Could not register font: {e}")
            font_name = None

    # get address via Gemini or fallback
    addr = None
    if (not force_fallback) and os.environ.get("GEMINI_API_KEY"):
        try:
            addr = call_gemini_for_address(city, api_key_env="GEMINI_API_KEY", debug=debug)
            if debug:
                st.write("Gemini address result:", addr)
        except Exception as e:
            if debug:
                st.write("call_gemini_for_address error:", e)
            addr = None
    if not addr:
        addr = get_random_address(city)
    # hotel phone: if user provided, use it, else pick from Gemini suggestions or random
    hotel_phone = hotel_phone_input.strip() or None

    # attempt to get hotels via Gemini to fill phone (internal), fallback if needed
    chosen_phone = None
    if not hotel_phone:
        hotels = None
        if not force_fallback and os.environ.get("GEMINI_API_KEY"):
            try:
                low = max(100, int(bill_amount * 0.8))
                high = int(bill_amount * 1.2)
                hotels = call_gemini_hotel_search(city, low, high, api_key_env="GEMINI_API_KEY", debug=debug)
                if debug:
                    st.write("Gemini hotel search result:", hotels)
            except Exception as e:
                if debug:
                    st.write("call_gemini_hotel_search error:", e)
                hotels = None
        if not hotels:
            hotels = fallback_hotel_suggestions(city, bill_amount)
        chosen = random.choice(hotels)
        chosen_phone = chosen.get("phone") or rand_mobile()
    hotel_phone_final = hotel_phone_input.strip() if hotel_phone_input.strip() else (chosen_phone or rand_mobile())

    gst_no = rand_gst_number()
    payment_mode = "Cash"

    # generate PDF bytes
    try:
        pdf_bytes = create_pdf_bytes(
            hotel_name=hotel_name,
            hotel_addr=addr,
            guest_name=guest_name,
            invoice_no=invoice_no,
            date_str=date_str,
            room_no=room_no,
            items=items,
            gst_no=gst_no,
            gst_percent=gst_percent,
            payment_mode=payment_mode,
            hotel_phone=hotel_phone_final,
            font_name=font_name,
            logo_file=hotel_logo
        )
    except Exception as e:
        st.error(f"Failed to generate PDF: {e}")
        raise

    fname = f"{hotel_name.replace(' ','_')}_bill.pdf"
    st.success("PDF generated!")
    st.download_button("Download invoice (PDF)", data=pdf_bytes, file_name=fname, mime="application/pdf")

    # cleanup tmp font file if created
    if font_tmp_path:
        try:
            os.remove(font_tmp_path)
        except Exception:
            pass

st.caption("Tip: set GEMINI_API_KEY in your environment to enable Gemini address/hotel suggestions. Use --force-fallback to skip Gemini.")
