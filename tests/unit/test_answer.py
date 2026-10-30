"""Cited answers without a model: which sentences are picked, and that citations only point at real passages."""

from swsdb_server.answer import citations, context, extractive, strip_unknown

HITS = [
    {"document": "refunds", "title": "Refund policy", "page": None, "heading": "Refunds",
     "text": "# Refunds\n\nRefunds are issued within five business days. Store credit is instant."},
    {"document": "shipping", "title": "Shipping", "page": 3, "heading": None,
     "text": "Orders ship from Rotterdam within 24 hours. Express delivery takes two days."},
]


def test_the_sentence_answering_the_question_is_picked_and_cited():
    a = extractive("How long do refunds take?", HITS)
    assert a.startswith("Refunds are issued within five business days.") and "[1]" in a
    assert "Rotterdam" not in a and "# Refunds" not in a
    b = extractive("Where do orders ship from?", HITS)
    assert "Rotterdam" in b and b.rstrip().endswith("[2]")


def test_a_heading_run_into_the_first_sentence_is_not_part_of_the_answer():
    hit = {"document": "refunds", "heading": "Refunds",
           "text": "# Refunds Refunds are issued within five business days. Store credit is instant."}
    assert extractive("How long do refunds take?", [hit]) == "Refunds are issued within five business days. [1]"
    nested = {"heading": "Shipping", "text": "## Express\nExpress parcels arrive in two days."}
    assert extractive("How fast are express parcels?", [nested]) == "Express parcels arrive in two days. [1]"


def test_extra_sentences_must_add_something_and_follow_the_chain():
    hits = [
        {"document": "project-orion", "text": "Project Orion is the new search platform, led by Priya Nair."},
        {"document": "project-helix", "text": "Amara Okafor leads the project."},
        {"document": "person-priya-nair",
         "text": "Priya Nair is a principal engineer who works from the Lisbon office."},
    ]
    a = extractive("Which office does the lead of Project Orion work from?", hits)
    assert a == ("Project Orion is the new search platform, led by Priya Nair. [1] "
                 "Priya Nair is a principal engineer who works from the Lisbon office. [3]")
    pricing = [
        {"document": "person-lena-fischer", "text": "She owns the product roadmap and signs off every pricing change."},
        {"document": "pricing", "text": "Enterprise pricing is agreed per contract."},
        {"document": "team-platform", "text": "Its on-call rotation changes every Monday."},
    ]
    assert extractive("Who approves pricing changes?", pricing) == \
        "She owns the product roadmap and signs off every pricing change. [1]"


def test_no_matching_words_means_no_answer_rather_than_a_guess():
    assert extractive("What is the capital of France?", HITS) == ""
    assert extractive("the and of", HITS) == ""
    assert extractive("refunds", []) == ""


def test_citations_are_in_order_and_only_real_passages():
    assert citations("Refunds take five days [2][1]. Shipping [2] and [9].", 2) == [2, 1]
    assert strip_unknown("Yes [1], also [7].", 2) == "Yes [1], also ."


def test_context_numbers_passages_with_where_they_come_from():
    c = context(HITS)
    assert c.startswith("[1] (Refund policy, Refunds)\n# Refunds")
    assert "[2] (Shipping, page 3)\nOrders ship" in c
