Test 1 — Clearly AI-generated (expect ~0.9)

$body = '{"text": "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment.", "creator_id": "test-user-1"}'
Invoke-RestMethod -Uri http://localhost:5000/submit -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json

Test 2 — Clearly human (expect ~0.1)

$body = '{"text": "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably wont go back unless someone drags me there", "creator_id": "test-user-2"}'
Invoke-RestMethod -Uri http://localhost:5000/submit -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json

Test 3 — Formal human writing (expect mid-high, ~0.7–0.8)

$body = '{"text": "The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension between their mandate for price stability and the unintended consequences of prolonged low interest rates on equity and real estate valuations.", "creator_id": "test-user-1"}'
Invoke-RestMethod -Uri http://localhost:5000/submit -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json

Test 4 — Lightly edited AI (expect mid-range, ~0.3–0.5)

$body = '{"text": "Ive been thinking a lot about remote work lately. There are genuine tradeoffs - flexibility and no commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity varies widely by individual and role type.", "creator_id": "test-user-2"}'
Invoke-RestMethod -Uri http://localhost:5000/submit -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json

Then check the full log:


Invoke-RestMethod -Uri http://localhost:5000/log | ConvertTo-Json -Depth 5