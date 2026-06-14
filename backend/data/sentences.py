#hardcoded sanskrit sentence for demo
"""
Hardcoded Sanskrit sentences for dev / demo.
Each sentence has everything needed for all three difficulty levels.
"""

from typing import TypedDict, Literal

Level = Literal["easy", "intermediate", "hard"]

class Sentence(TypedDict):
    id: int
    sanskrit: str           # Devanagari text shown in karaoke panel
    transliteration: str    # Roman transliteration (helper)
    meaning_hi: str         # Hindi meaning
    meaning_en: str         # English meaning
    translation_en: str     # Expected English translation (for checking)
    grammar_note: str       # Grammar question asked on hard level
    level: Level


SENTENCES: dict[Level, list[Sentence]] = {
    "easy": [
        {
            "id": 1,
            "sanskrit": "अहं पठामि।",
            "transliteration": "ahaṃ paṭhāmi",
            "meaning_hi": "मैं पढ़ता हूँ।",
            "meaning_en": "I read / I study.",
            "translation_en": "I read.",
            "grammar_note": "अहम् किस विभक्ति में है?",
            "level": "easy",
        },
        {
            "id": 2,
            "sanskrit": "सः बालकः अस्ति।",
            "transliteration": "saḥ bālakaḥ asti",
            "meaning_hi": "वह लड़का है।",
            "meaning_en": "He is a boy.",
            "translation_en": "He is a boy.",
            "grammar_note": "बालकः में कौन सी विभक्ति है?",
            "level": "easy",
        },
        {
            "id": 3,
            "sanskrit": "जलं पिबामि।",
            "transliteration": "jalaṃ pibāmi",
            "meaning_hi": "मैं पानी पीता हूँ।",
            "meaning_en": "I drink water.",
            "translation_en": "I drink water.",
            "grammar_note": "जलम् का कारक क्या है?",
            "level": "easy",
        },
        {
            "id": 4,
            "sanskrit": "सा सुन्दरी अस्ति।",
            "transliteration": "sā sundarī asti",
            "meaning_hi": "वह सुंदर है।",
            "meaning_en": "She is beautiful.",
            "translation_en": "She is beautiful.",
            "grammar_note": "सा का अर्थ क्या है?",
            "level": "easy",
        },
        {
            "id": 5,
            "sanskrit": "वयं खादामः।",
            "transliteration": "vayaṃ khādāmaḥ",
            "meaning_hi": "हम खाते हैं।",
            "meaning_en": "We eat.",
            "translation_en": "We eat.",
            "grammar_note": "वयम् किस पुरुष का रूप है?",
            "level": "easy",
        },
    ],
    "intermediate": [
        {
            "id": 6,
            "sanskrit": "रामः वनं गच्छति।",
            "transliteration": "rāmaḥ vanaṃ gacchati",
            "meaning_hi": "राम वन को जाता है।",
            "meaning_en": "Rama goes to the forest.",
            "translation_en": "Rama goes to the forest.",
            "grammar_note": "वनम् में कौन सी विभक्ति है?",
            "level": "intermediate",
        },
        {
            "id": 7,
            "sanskrit": "बालकः पुस्तकं पठति।",
            "transliteration": "bālakaḥ pustakaṃ paṭhati",
            "meaning_hi": "लड़का किताब पढ़ता है।",
            "meaning_en": "The boy reads a book.",
            "translation_en": "The boy reads a book.",
            "grammar_note": "पुस्तकम् का कारक क्या है?",
            "level": "intermediate",
        },
        {
            "id": 8,
            "sanskrit": "माता पुत्राय भोजनं ददाति।",
            "transliteration": "mātā putrāya bhojanaṃ dadāti",
            "meaning_hi": "माँ बेटे को खाना देती है।",
            "meaning_en": "The mother gives food to the son.",
            "translation_en": "The mother gives food to the son.",
            "grammar_note": "पुत्राय में कौन सी विभक्ति और कारक है?",
            "level": "intermediate",
        },
        {
            "id": 9,
            "sanskrit": "गुरुः शिष्यान् पाठयति।",
            "transliteration": "guruḥ śiṣyān pāṭhayati",
            "meaning_hi": "गुरु शिष्यों को पढ़ाता है।",
            "meaning_en": "The teacher teaches the students.",
            "translation_en": "The teacher teaches the students.",
            "grammar_note": "शिष्यान् किस वचन में है?",
            "level": "intermediate",
        },
    ],
    "hard": [
        {
            "id": 10,
            "sanskrit": "योगः कर्मसु कौशलम्।",
            "transliteration": "yogaḥ karmasu kauśalam",
            "meaning_hi": "योग कर्मों में कुशलता है।",
            "meaning_en": "Yoga is skill in actions.",
            "translation_en": "Yoga is skill in actions.",
            "grammar_note": "कर्मसु में कौन सी विभक्ति है और क्यों?",
            "level": "hard",
        },
        {
            "id": 11,
            "sanskrit": "विद्या ददाति विनयम्।",
            "transliteration": "vidyā dadāti vinayam",
            "meaning_hi": "विद्या विनम्रता देती है।",
            "meaning_en": "Knowledge bestows humility.",
            "translation_en": "Knowledge bestows humility.",
            "grammar_note": "विनयम् का मूल शब्द और विभक्ति बताइए।",
            "level": "hard",
        },
        {
            "id": 12,
            "sanskrit": "सत्यमेव जयते।",
            "transliteration": "satyameva jayate",
            "meaning_hi": "सत्य की ही जीत होती है।",
            "meaning_en": "Truth alone triumphs.",
            "translation_en": "Truth alone triumphs.",
            "grammar_note": "एव का व्याकरणिक कार्य क्या है?",
            "level": "hard",
        },
    ],
}


def get_sentences(level: Level) -> list[Sentence]:
    return SENTENCES[level]


def get_sentence_by_id(sentence_id: int) -> Sentence | None:
    for sentences in SENTENCES.values():
        for s in sentences:
            if s["id"] == sentence_id:
                return s
    return None