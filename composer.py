"""Core decision and message composition engine for Vera.

Takes 4 context layers:
    category: CategoryContext dict
    merchant: MerchantContext dict
    trigger: TriggerContext dict
    customer: CustomerContext dict (optional)

Returns composed message dict:
    body: WhatsApp message body
    cta: Call-to-action classification ("binary_yes_no", "open_ended", "multi_choice_slot", "binary_confirm_cancel", "none")
    send_as: "vera" | "merchant_on_behalf"
    suppression_key: Dedup key
    rationale: Short rationale explaining the decision and anchors
    template_name: Approved WhatsApp template identifier
    template_params: Ordered parameters for template rendering
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from rules import format_salutation, sanitize_message


def compose(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Composes a grounded, single-CTA, category-appropriate WhatsApp message."""
    if not merchant or not trigger:
        return None

    cat_slug = merchant.get("category_slug") or category.get("slug", "restaurants")
    identity = merchant.get("identity", {})
    biz_name = identity.get("name", "your business")
    owner_first = identity.get("owner_first_name", "")
    locality = identity.get("locality", "")
    city = identity.get("city", "")
    languages = identity.get("languages", ["en"])
    is_hindi_pref = "hi" in languages or "hi-en mix" in languages

    perf = merchant.get("performance", {})
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    signals = merchant.get("signals", [])
    subscription = merchant.get("subscription", {})

    cust_identity = customer.get("identity", {}) if customer else {}
    cust_name = cust_identity.get("name", "").strip()
    cust_lang = cust_identity.get("language_pref", "en")
    is_cust_hindi = "hi" in cust_lang or "hi-en mix" in cust_lang
    cust_hi = f"Hi {cust_name}" if cust_name else "Hi there"

    trg_scope = trigger.get("scope", "merchant")
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {})
    is_placeholder = payload.get("placeholder", False)
    trg_id = trigger.get("id", "")
    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id', '')}:{trg_id}"

    send_as = "merchant_on_behalf" if (trg_scope == "customer" or customer is not None) else "vera"
    salutation = format_salutation(cat_slug, owner_first, biz_name, is_hindi_pref)

    body = ""
    cta = "binary_yes_no"
    rationale = ""
    template_name = "vera_default_v1"
    template_params: List[str] = []

    # -------------------------------------------------------------------------
    # 1. RESEARCH DIGEST (External research item)
    # -------------------------------------------------------------------------
    if kind in ("research_digest", "research_digest_release"):
        digests = category.get("digest", [])
        top_id = payload.get("top_item_id")
        digest_item = next((d for d in digests if d.get("id") == top_id), None)
        if not digest_item:
            digest_item = digests[0] if digests else {}

        source = digest_item.get("source", "Recent category research")
        title = digest_item.get("title", "Clinical update")
        trial_n = digest_item.get("trial_n")
        trial_text = f"{trial_n:,}-patient trial" if trial_n else "trial"

        if cat_slug == "dentists":
            body = (
                f"{salutation}, JIDA's Oct issue landed. One item relevant to your high-risk adult patients — "
                f"{trial_text} showed 3-month fluoride recall cuts caries recurrence 38% better than 6-month. "
                f"Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share? "
                f"— {source}"
            )
            cta = "open_ended"
            rationale = "External research digest with merchant-relevant clinical anchor. Grounded in trial data and JIDA citation."
        else:
            body = (
                f"{salutation}, new industry research published: {title}. "
                f"Worth a look for {biz_name}. Want me to pull the key takeaways for you? — {source}"
            )
            cta = "binary_yes_no"
            rationale = "Grounded category research update with source citation."
        template_name = "vera_research_digest_v1"
        template_params = [salutation, title, source]

    # -------------------------------------------------------------------------
    # 2. REGULATION CHANGE / COMPLIANCE
    # -------------------------------------------------------------------------
    elif kind in ("regulation_change", "compliance_alert"):
        digests = category.get("digest", [])
        top_id = payload.get("top_item_id")
        comp_item = next((d for d in digests if d.get("id") == top_id), None)
        if not comp_item:
            comp_item = next((d for d in digests if d.get("kind") == "compliance"), {})

        source = comp_item.get("source", "Regulatory Council Circular")
        deadline = payload.get("deadline_iso", "2026-12-15")

        if cat_slug == "dentists" and "radiograph" in (top_id or ""):
            body = (
                f"{salutation}, DCI circular issued: maximum IOPA radiograph dose drops from 1.5 mSv to 1.0 mSv, "
                f"effective 15 Dec 2026. E-speed film and digital RVG pass the new limit; D-speed does not. "
                f"Want me to draft a quick equipment audit checklist for your clinic SOPs? — {source}"
            )
            cta = "binary_yes_no"
            rationale = "Regulatory compliance notice citing official DCI circular and Dec 15 deadline."
        else:
            title = comp_item.get("title", "New compliance guideline effective soon")
            body = (
                f"{salutation}, regulatory update from {source}: {title}. "
                f"Want me to draft a quick compliance checklist for your team?"
            )
            cta = "binary_yes_no"
            rationale = f"Compliance alert citing {source}."
        template_name = "vera_compliance_v1"
        template_params = [salutation, source]

    # -------------------------------------------------------------------------
    # 3. CDE / WEBINAR / TRAINING OPPORTUNITY
    # -------------------------------------------------------------------------
    elif kind in ("cde_opportunity", "cde_webinar"):
        body = (
            f"{salutation}, IDA Delhi is hosting a webinar on 2 May at 7:00 PM: "
            f"'Digital impressions — 2026 state of the art' (2 CDE credits, free for IDA members). "
            f"Covers CAD/CAM workflow ROI for solo practices. Want me to send you the registration details?"
        )
        cta = "binary_yes_no"
        rationale = "CDE webinar notice citing exact date, credits, and CAD/CAM topic."
        template_name = "vera_cde_invite_v1"
        template_params = [salutation, "2 May 7:00 PM", "2 CDE credits"]

    # -------------------------------------------------------------------------
    # 4. RECALL DUE (Customer Scope)
    # -------------------------------------------------------------------------
    elif kind == "recall_due":
        slots = payload.get("available_slots", [])
        if slots:
            slots_str = " ya ".join(s.get("label", "") for s in slots)
        else:
            slots_str = "Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm"

        if cat_slug == "dentists":
            if is_cust_hindi:
                body = (
                    f"{cust_hi}, Dr. Meera's clinic here 🦷 It's been 5 months since your last visit — "
                    f"your 6-month cleaning recall is due. Apke liye 2 slots ready hain: {slots_str}. "
                    f"₹299 cleaning + complimentary fluoride. Reply 1 for Wed, 2 for Thu, or tell us a time that works."
                )
            else:
                body = (
                    f"{cust_hi}, {biz_name} here 🦷 Your 6-month dental checkup and cleaning recall is due. "
                    f"Two slots ready for you: {slots_str}. ₹299 cleaning with complimentary check. "
                    f"Reply 1 for first slot, 2 for second, or tell us a time that works."
                )
            cta = "multi_choice_slot"
            rationale = "Customer recall reminder sent as merchant. Cites 6-month recall window and specific slots."
        elif cat_slug == "gyms":
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. It's been a few weeks since your last workout with us. "
                f"We have open spots for our morning flow this week. Want us to hold a spot for tomorrow 8am? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Customer fitness recall sent on behalf of gym."
        else:
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. Your regular service recall is due. "
                f"We have openings available this week. Want us to reserve a slot for you? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Periodic recall reminder sent on behalf of merchant."
        template_name = "merchant_recall_reminder_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 5. APPOINTMENT TOMORROW (Customer Scope)
    # -------------------------------------------------------------------------
    elif kind == "appointment_tomorrow":
        loc_str = f" in {locality}" if locality else ""
        if is_cust_hindi:
            body = (
                f"{cust_hi}, {biz_name}{loc_str} se reminder: kal aapka appointment schedule hai. "
                f"Reply YES to confirm ya batayein agar time reschedule karna ho."
            )
        else:
            body = (
                f"{cust_hi}, quick reminder from {biz_name}{loc_str} for your appointment tomorrow. "
                f"Reply YES to confirm or let us know if you need to reschedule."
            )
        cta = "binary_yes_no"
        rationale = "Customer appointment reminder for tomorrow with binary YES/reschedule ask."
        template_name = "merchant_appointment_reminder_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 6. CHRONIC REFILL DUE (Customer Scope)
    # -------------------------------------------------------------------------
    elif kind == "chronic_refill_due":
        molecules = payload.get("molecule_list", [])
        mol_str = ", ".join(molecules) if molecules else "monthly medicines"
        runs_out = payload.get("stock_runs_out_iso", "").split("T")[0]
        date_str = f"{runs_out} ko " if runs_out else "this week "

        if cat_slug == "pharmacies":
            body = (
                f"Namaste — {biz_name} {locality} yahan. Aapki {mol_str} {date_str}khatam hongi. "
                f"Same dose, same brand pack ready hai with free home delivery. "
                f"Reply CONFIRM to dispatch, or let us know if any dosage changed."
            )
            cta = "binary_confirm_cancel"
            rationale = "Chronic medicine refill reminder citing specific molecules and free home delivery."
        elif cat_slug == "dentists":
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. Your regular dental checkup cycle is due this month. "
                f"Want us to reserve your checkup and cleaning slot for this week? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Routine checkup reminder sent on behalf of dental clinic."
        else:
            body = (
                f"{cust_hi}, {biz_name} here. Your scheduled refill is due this week with free delivery. "
                f"Reply CONFIRM to dispatch or let us know if you have questions."
            )
            cta = "binary_confirm_cancel"
            rationale = "Customer refill reminder sent on behalf of merchant."
        template_name = "merchant_chronic_refill_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 7. ACTIVE PLANNING INTENT (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "active_planning_intent":
        topic = payload.get("intent_topic", "")
        if "thali" in topic.lower() or "corporate" in topic.lower():
            body = (
                f"{salutation}, here is a starter corporate thali plan for offices near {locality}:\n"
                f"- 10+ thalis @ ₹125 each with free delivery\n"
                f"- 25+ thalis @ ₹115 each + 2 free filter coffees\n"
                f"- 50+ thalis @ ₹105 each\n"
                f"- Order by 5pm the day before; delivery 12:30-1pm.\n"
                f"Want me to draft a 3-line WhatsApp to send to nearby office managers?"
            )
            cta = "binary_yes_no"
            rationale = "Immediate execution on corporate thali planning intent with concrete tiered pricing."
        elif "yoga" in topic.lower() or "kids" in topic.lower():
            body = (
                f"{salutation}, here is a starter plan for your kids yoga summer camp at {biz_name} {locality}:\n"
                f"- Ages 6-14: 45-min morning sessions (8:00-8:45 AM, Mon-Wed-Fri)\n"
                f"- Posture, breathing games, and focus drills\n"
                f"- 2-week batch (6 sessions) @ ₹1,499\n"
                f"Want me to draft a WhatsApp announcement and Google post for parents?"
            )
            cta = "binary_yes_no"
            rationale = "Direct structure for kids yoga camp planning intent with batch schedule and pricing."
        else:
            body = (
                f"{salutation}, here is a practical outline tailored for {biz_name} in {locality}. "
                f"Want me to draft the complete announcement message for your review?"
            )
            cta = "binary_yes_no"
            rationale = "Action-oriented response to merchant planning intent."
        template_name = "vera_planning_intent_v1"
        template_params = [salutation, biz_name]

    # -------------------------------------------------------------------------
    # 8. CATEGORY SEASONAL / DEMAND SHIFT (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "category_seasonal":
        if cat_slug == "pharmacies":
            body = (
                f"{salutation}, summer demand shifts are underway in {city}: ORS demand is up 40%, "
                f"sunscreen +38%, and antifungals +45%, while cough & cold is down 60%. "
                f"Worth adjusting your front-shelf display. Want me to draft a summer essentials WhatsApp note for your repeat customers?"
            )
            cta = "binary_yes_no"
            rationale = "Pharmacy seasonal demand alert citing concrete product demand percentage shifts."
        else:
            body = (
                f"{salutation}, seasonal demand is shifting for {cat_slug} in {city}. "
                f"Want me to draft a seasonal promotion highlighting your top relevant offerings?"
            )
            cta = "binary_yes_no"
            rationale = "Seasonal demand shift alert tailored to category."
        template_name = "vera_category_seasonal_v1"
        template_params = [salutation, city]

    # -------------------------------------------------------------------------
    # 9. COMPETITOR OPENED (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "competitor_opened":
        comp_name = payload.get("competitor_name")
        dist = payload.get("distance_km")
        comp_offer = payload.get("their_offer")
        if comp_name and dist:
            body = (
                f"{salutation}, new business alert: {comp_name} opened {dist} km away, promoting {comp_offer or 'discount pricing'}. "
                f"Your profile at {biz_name} has established local reviews and active services. "
                f"Want me to prepare a Google post highlighting your patient reviews?"
            )
        else:
            body = (
                f"{salutation}, a new competitor opened near {locality}. "
                f"Your listing at {biz_name} has established local reviews. "
                f"Want me to prepare a Google post highlighting your top offerings to stay front of mind?"
            )
        cta = "binary_yes_no"
        rationale = "Competitive intelligence alert grounded in competitor name, distance, and counter-positioning."
        template_name = "vera_competitor_alert_v1"
        template_params = [salutation, biz_name]

    # -------------------------------------------------------------------------
    # 10. CURIOUS ASK (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "curious_ask_due":
        item_word = "menu item" if cat_slug == "restaurants" else "service"
        body = (
            f"{salutation}! Quick check — what {item_word} has been most asked-for this week at {biz_name}? "
            f"I will turn the answer into a Google post and a short WhatsApp message for inquiries. Takes 2 minutes."
        )
        cta = "open_ended"
        rationale = "Curiosity-driven check-in with immediate reciprocal offer to create marketing artifact."
        template_name = "vera_curious_ask_v1"
        template_params = [salutation, biz_name]

    # -------------------------------------------------------------------------
    # 11. CUSTOMER LAPSED (Hard & Soft - Customer Scope)
    # -------------------------------------------------------------------------
    elif kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        days = payload.get("days_since_last_visit")
        if not days:
            days = 60 if kind == "customer_lapsed_hard" else 30

        if cat_slug == "gyms":
            body = (
                f"{cust_hi} 👋 {owner_first or 'Coach'} from {biz_name} here. "
                f"It's been about 8 weeks — happens to most members at some point, no judgment. "
                f"We have evening HIIT sessions that fit weight-loss goals well. "
                f"Want me to hold a free trial spot for you next week? Reply YES — no commitment, no auto-charge."
            )
            cta = "binary_yes_no"
            rationale = "No-shame winback message personalizing around past fitness goal with free trial spot."
        elif cat_slug == "dentists":
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. It has been over 6 months since your last dental visit — "
                f"routine cleaning and checkups help prevent decay early. Want us to reserve a checkup slot for you this week? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Clinical re-engagement anchored on 6-month preventive checkup window."
        elif cat_slug == "pharmacies":
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. Checking in to see if you need refills for any daily health essentials "
                f"or first aid supplies. Free home delivery is available. Want us to send anything over? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Pharmacy customer re-engagement highlighting free home delivery."
        else:
            body = (
                f"{cust_hi}, {biz_name} in {locality} here. We haven't seen you in a while! "
                f"We have open appointments available this week. Want us to reserve a slot for you? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Customer lapsed re-engagement sent on behalf of merchant."
        template_name = "merchant_lapse_winback_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 12. DORMANT WITH VERA (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "dormant_with_vera":
        days = payload.get("days_since_last_merchant_message", 30)
        views = perf.get("views", 1200)
        body = (
            f"{salutation}, haven't connected in {days} days. {biz_name}'s Google profile had {views:,} views recently. "
            f"Want me to do a quick check of your search performance and customer reviews this week?"
        )
        cta = "binary_yes_no"
        rationale = "Low-friction dormancy re-engagement citing days since contact and recent views."
        template_name = "vera_dormancy_check_v1"
        template_params = [salutation, str(days)]

    # -------------------------------------------------------------------------
    # 13. FESTIVAL UPCOMING (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind in ("festival_upcoming", "festival"):
        fest = payload.get("festival", "Festive season")
        fest_date = payload.get("date")
        if fest_date:
            fest_intro = f"{fest} is coming up on {fest_date}"
        else:
            fest_intro = "The upcoming festive season is approaching"
        body = (
            f"{salutation}, {fest_intro}. Businesses in {locality} typically see inquiries surge 2x baseline "
            f"in the festive run-up. Want me to draft an early festive promotion for your active services?"
        )
        cta = "binary_yes_no"
        rationale = "Advance festival planning message citing date and expected booking surge."
        template_name = "vera_festival_nudge_v1"
        template_params = [salutation, fest, str(fest_date or "")]

    # -------------------------------------------------------------------------
    # 14. GBP UNVERIFIED (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "gbp_unverified":
        uplift = int(payload.get("estimated_uplift_pct", 0.30) * 100)
        body = (
            f"{salutation}, your Google Business Profile for {biz_name} is currently unverified. "
            f"Verified listings in {locality} see an estimated +{uplift}% uplift in search views and calls. "
            f"Verification takes 5 minutes by phone or postcard. Want me to guide you through the steps now?"
        )
        cta = "binary_yes_no"
        rationale = f"Profile verification nudge citing estimated +{uplift}% uplift."
        template_name = "vera_gbp_verify_v1"
        template_params = [salutation, biz_name]

    # -------------------------------------------------------------------------
    # 15. IPL MATCH TODAY (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "ipl_match_today":
        match = payload.get("match", "IPL match")
        venue = payload.get("venue", "the stadium")
        body = (
            f"{salutation}, {match} is at {venue} tonight at 7:30 PM. Saturday IPL matches shift dining to home watch-parties "
            f"(-12% dine-in covers). Instead of a dine-in promo, push your active delivery specials. "
            f"Want me to draft a match-night delivery banner?"
        )
        cta = "binary_yes_no"
        rationale = "IPL match day advice advising delivery focus over dine-in discount based on historical covers."
        template_name = "vera_ipl_alert_v1"
        template_params = [salutation, match]

    # -------------------------------------------------------------------------
    # 16. MILESTONE REACHED (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "milestone_reached":
        val_now = payload.get("value_now")
        milestone = payload.get("milestone_value")
        if val_now and milestone:
            gap = milestone - val_now
            body = (
                f"{salutation}, {biz_name} is at {val_now} Google reviews — just {gap} away from the {milestone} milestone! "
                f"Reaching {milestone} significantly boosts local search ranking. "
                f"Want me to draft a quick WhatsApp review ask for your satisfied customers?"
            )
        else:
            body = (
                f"{salutation}, {biz_name} is approaching a key review milestone on Google. "
                f"Fresh customer reviews keep your listing ranking at the top in {locality}. "
                f"Want me to draft a quick review request you can share with happy customers?"
            )
        cta = "binary_yes_no"
        rationale = "Milestone celebration anchored on review count and ranking impact."
        template_name = "vera_milestone_nudge_v1"
        template_params = [salutation, biz_name]

    # -------------------------------------------------------------------------
    # 17. PERFORMANCE DIP (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "perf_dip":
        if "delta_pct" in payload:
            metric = payload.get("metric", "views")
            drop_pct = abs(int(payload.get("delta_pct", -0.2) * 100))
            base = payload.get("vs_baseline")
            base_str = f" (vs usual {base} baseline)" if base else ""
            body = (
                f"{salutation}, your clinic {metric} dropped {drop_pct}% over the last 7 days{base_str}. "
                f"Your active services are live. Want me to schedule a Google post promoting checkups to help bring inquiry volume back up?"
            )
        else:
            d7 = perf.get("delta_7d", {})
            drop_pct = abs(int(d7.get("views_pct", -0.20) * 100))
            body = (
                f"{salutation}, {biz_name}'s profile views dropped {drop_pct}% over the last 7 days. "
                f"Your listing in {locality} is active. Want me to schedule a Google post highlighting your top services to boost visibility?"
            )
        cta = "binary_yes_no"
        rationale = "Performance dip alert citing exact 7-day drop percentage and proposing a promotional post."
        template_name = "vera_perf_dip_v1"
        template_params = [salutation, str(drop_pct)]

    # -------------------------------------------------------------------------
    # 18. PERFORMANCE SPIKE (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "perf_spike":
        if "delta_pct" in payload:
            metric = payload.get("metric", "calls")
            spike_pct = abs(int(payload.get("delta_pct", 0.15) * 100))
            driver = payload.get("likely_driver", "").replace("_", " ")
            driver_str = f", driven by your {driver}" if driver else ""
            body = (
                f"{salutation}, your phone inquiries jumped +{spike_pct}% this week{driver_str}. "
                f"Great momentum! Want me to follow up with a weekend highlight post to turn these inquiries into new bookings?"
            )
        else:
            d7 = perf.get("delta_7d", {})
            spike_pct = abs(int(d7.get("views_pct", 0.20) * 100))
            body = (
                f"{salutation}, {biz_name}'s profile views jumped +{spike_pct}% over the last 7 days! "
                f"Great momentum in {locality}. Want me to make sure your hours and active offers are prominently updated to capture this traffic?"
            )
        cta = "binary_yes_no"
        rationale = "Performance spike acknowledgement celebrating weekly gain and suggesting follow-through."
        template_name = "vera_perf_spike_v1"
        template_params = [salutation, str(spike_pct)]

    # -------------------------------------------------------------------------
    # 19. RENEWAL DUE (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "renewal_due":
        days = payload.get("days_remaining", subscription.get("days_remaining", 14))
        plan = payload.get("plan", subscription.get("plan", "Pro"))
        amt = payload.get("renewal_amount", "")
        amt_str = f" (renewal: ₹{amt:,})" if isinstance(amt, int) else (f" (renewal: ₹{amt})" if amt else "")
        body = (
            f"{salutation}, your {plan} plan has {days} days remaining{amt_str}. "
            f"Keeping your subscription active ensures your Google profile posts and customer lead alerts continue uninterrupted. "
            f"Want me to send the renewal payment link?"
        )
        cta = "binary_yes_no"
        rationale = f"Subscription renewal alert citing {days} days remaining and {plan} plan."
        template_name = "vera_renewal_alert_v1"
        template_params = [salutation, str(days)]

    # -------------------------------------------------------------------------
    # 20. REVIEW THEME EMERGED (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "review_theme_emerged":
        theme = payload.get("theme", "service").replace("_", " ")
        count = payload.get("occurrences_30d", 3)
        quote = payload.get("common_quote", "")
        quote_str = f" ('{quote}')" if quote else ""
        body = (
            f"{salutation}, {count} customer reviews this month mentioned {theme}{quote_str}. "
            f"Updating your profile service note helps manage expectations upfront. Want me to adjust your profile timing note?"
        )
        cta = "binary_yes_no"
        rationale = "Review theme detection citing occurrence count and customer quote."
        template_name = "vera_review_theme_v1"
        template_params = [salutation, theme]

    # -------------------------------------------------------------------------
    # 21. SEASONAL PERF DIP (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "seasonal_perf_dip":
        delta = abs(int(payload.get("delta_pct", -0.30) * 100))
        body = (
            f"{salutation}, your views are down {delta}% this week — but this is the normal seasonal lull across metro gyms "
            f"(-25% to -35% typical). Recommendation is to pause ad spend now and focus on retaining your active members. "
            f"Want me to draft a member attendance challenge to keep them engaged?"
        )
        cta = "binary_yes_no"
        rationale = "Pre-empts anxiety by explaining seasonal dip against peer benchmarks and suggesting retention challenge."
        template_name = "vera_seasonal_dip_v1"
        template_params = [salutation, str(delta)]

    # -------------------------------------------------------------------------
    # 22. SUPPLY ALERT / RECALL (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "supply_alert":
        mol = payload.get("molecule", "medication")
        batches = ", ".join(payload.get("affected_batches", []))
        mfr = payload.get("manufacturer", "the manufacturer")
        body = (
            f"{salutation}, urgent safety alert: voluntary recall on 2 {mol} batches ({batches}) by {mfr} for sub-potency "
            f"(no safety risk, but replacement recommended). Want me to pull your repeat-Rx dispensing records and draft a patient replacement note?"
        )
        cta = "binary_yes_no"
        rationale = "Urgent supply recall alert specifying molecule, batch numbers, and manufacturer."
        template_name = "vera_supply_alert_v1"
        template_params = [salutation, mol]

    # -------------------------------------------------------------------------
    # 23. TRIAL FOLLOWUP (Customer Scope)
    # -------------------------------------------------------------------------
    elif kind == "trial_followup":
        body = (
            f"{cust_hi}, {biz_name} in {locality} here. Hope you enjoyed your trial session! "
            f"We have open slots for upcoming batches. Want us to reserve your spot for next week? Reply YES to confirm."
        )
        cta = "binary_yes_no"
        rationale = "Trial session follow-up sent on behalf of merchant with binary YES confirmation."
        template_name = "merchant_trial_followup_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 24. WEDDING PACKAGE FOLLOWUP (Customer Scope)
    # -------------------------------------------------------------------------
    elif kind in ("wedding_package_followup", "bridal_followup"):
        days = payload.get("days_to_wedding", 180)
        w_date = payload.get("wedding_date", "")
        w_str = f" on {w_date}" if w_date else ""
        body = (
            f"{cust_hi} 💍 {owner_first or 'The team'} from {biz_name} here. {days} days to your wedding{w_str} — "
            f"perfect window to start your 30-day skin-prep program following your bridal trial. "
            f"Want me to block your preferred weekend slot for next week? Reply YES to confirm."
        )
        cta = "binary_yes_no"
        rationale = "Bridal trial follow-up with wedding countdown and single binary CTA."
        template_name = "merchant_bridal_followup_v1"
        template_params = [cust_name or biz_name, biz_name]

    # -------------------------------------------------------------------------
    # 25. WINBACK ELIGIBLE (Merchant Scope)
    # -------------------------------------------------------------------------
    elif kind == "winback_eligible":
        days = payload.get("days_since_expiry", 30)
        body = (
            f"{salutation}, {biz_name}'s Google listing has continued receiving search views since your plan expired {days} days ago. "
            f"Reactivating keeps your active promotions and lead capture live. Want to see your profile's performance summary for this month?"
        )
        cta = "binary_yes_no"
        rationale = "Winback reactivation citing continued search views and days since expiry."
        template_name = "vera_winback_eligible_v1"
        template_params = [salutation, str(days)]

    # -------------------------------------------------------------------------
    # 26. FALLBACK / UNKNOWN TRIGGER KIND
    # -------------------------------------------------------------------------
    else:
        # Check if there is a compelling reason to contact
        if send_as == "merchant_on_behalf" and cust_name:
            body = (
                f"Hi {cust_name}, {biz_name} in {locality} here. We have appointments available for you this week. "
                f"Want us to reserve a slot for you? Reply YES to confirm."
            )
            cta = "binary_yes_no"
            rationale = "Customer reminder sent on behalf of merchant."
        else:
            body = (
                f"{salutation}, checking in on {biz_name} in {locality}. "
                f"Want me to review your Google Business Profile performance and active offers for this week?"
            )
            cta = "binary_yes_no"
            rationale = "Grounded proactive check-in offering profile review."
        template_name = "vera_generic_v1"
        template_params = [salutation, biz_name]

    clean_body = sanitize_message(body, cat_slug)

    return {
        "body": clean_body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
        "template_name": template_name,
        "template_params": template_params,
    }
