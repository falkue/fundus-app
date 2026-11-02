"""Curated publisher types for publishers whose type importers could not infer. Store.publisher() applies them
when a publisher has no type; `fundus normalise` fills existing rows."""

TYPES = {
    # media
    **dict.fromkeys(["arabian-marketer", "business-am", "china-daily", "co-ai", "cointelegraph", "computerwoche",
                     "marketing-interactive", "technical-education-post", "the-impression", "dark-reading",
                     "digital-dealer", "electrek", "le-10-minutes-hotelier", "motley-fool-transcript", "skift",
                     "techradar", "the-decoder"], "media"),
    # official bodies, regulators, parliaments
    **dict.fromkeys(["california", "garante-per-la-protezione-dei-dati-personali", "anpd", "cai",
                     "colorado-general-assembly", "cppa", "crtc", "dutch-ap", "enisa", "npc", "pipc", "sdaia",
                     "switzerland", "uk-parliament"], "official"),
    # academic
    **dict.fromkeys(["tu-wien-academy", "aea-rct-registry", "brynjolfsson", "huang-rust", "kaiser-and-schulze"],
                    "academic"),
    # consultancies, law firms, professional services
    **dict.fromkeys(["fenwick", "talan", "gunderson-dettmer", "jll", "paul-weiss", "wolk-nikolic"], "consultancy"),
    # analysts and market research
    **dict.fromkeys(["brandon-hall-group", "morgan-stanley", "stratistics-mrc", "ypulse", "edison-research"], "analyst"),
    # vendors selling what they measure
    **dict.fromkeys(["rocked", "attensi", "retorio", "salesforce-publicis-sapient", "workflow-labs"], "vendor"),
    # companies
    **dict.fromkeys(["skoda-storyboard", "autotrader", "byd-europe-careers", "heicoders-academy-case-study",
                     "jobteaser", "luxury-bazaar", "tuv-nord", "vertex-ventures", "berkshire-hathaway-automotive"],
                    "company"),
    # associations
    "arf-summary": "association",
    # unclear
    **dict.fromkeys(["castelo", "dion-arnould", "iads-search-summaries", "raj", "regulation"], "other"),
}
