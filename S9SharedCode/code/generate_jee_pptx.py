from pptx import Presentation

prs = Presentation()

slide_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(slide_layout)
title = slide.shapes.title
subtitle = slide.placeholders[1]
title.text = "JEE Preparation Interactive Panel"
subtitle.text = "Comprehensive Practice, Problem-Solving Exercises, and Self-Assessment Tools\nPhysics, Chemistry, and Mathematics"

slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Interactive Panel Overview"
body = slide.placeholders[1]
body.text = (
    "- Unified Dashboard for Physics, Chemistry, and Mathematics\n"
    "- Multiple Choice Questions (MCQ) with NTA Marking (+4 / -1)\n"
    "- Numerical Value Questions with Exact Value Input (+4 / 0)\n"
    "- Step-by-Step Solutions & Instant Feedback\n"
    "- Real-Time CBT Simulation with Live Timer & Analytics"
)

slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Physics: Core Topics & Practice"
body = slide.placeholders[1]
body.text = (
    "- Core Weightage Chapters: Thermodynamics, Electrostatics, Mechanics, Rotational Motion\n"
    "- Problem Types: Conceptual MCQs, Multi-concept numericals, Experimental physics\n"
    "- Sample Exercise (Mechanics): A block of mass m slides down an inclined plane...\n"
    "- Interactive Tools: Formula notes, vector visualizer, instant error analysis"
)

slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Chemistry: Core Topics & Practice"
body = slide.placeholders[1]
body.text = (
    "- Core Weightage Chapters: Coordination Compounds, Chemical Bonding, d- & f-Block Elements\n"
    "- Division: Physical Chemistry (numerical problems), Organic (reaction mechanisms), Inorganic (trends)\n"
    "- Sample Exercise (Coordination): IUPAC naming and crystal field stabilization energy (CFSE)\n"
    "- Interactive Tools: Periodic table calculator, reaction pathway drillers"
)

slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Mathematics: Core Topics & Practice"
body = slide.placeholders[1]
body.text = (
    "- Core Weightage Chapters: Definite Integration, 3D Geometry, Complex Numbers, Matrices\n"
    "- Problem Types: Analytical problem solving, shortcut applications, proof-based exercises\n"
    "- Sample Exercise (Calculus): Evaluating definite integrals using properties and substitutions\n"
    "- Interactive Tools: Graphing calculator, step algebraic simplifier"
)

slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Self-Assessment & Mock Test Engine"
body = slide.placeholders[1]
body.text = (
    "- Chapter-wise Test Modules: Immediate testing after completing each micro-topic\n"
    "- PYQ Integration (2013–2026): Filter questions by year, shift, and difficulty\n"
    "- Performance Analytics: Percentile tracking, time spent per question, weak-area identification\n"
    "- Offline/Local Storage: Automatic local progress saving across sessions"
)

prs.save("JEE_Preparation_Interactive_Panel.pptx")
print("Successfully generated JEE_Preparation_Interactive_Panel.pptx")
