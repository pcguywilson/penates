@echo off
cd /d "%~dp0"
echo ==== new-URL dry sweep  %DATE% %TIME% ==== > tool_test_output.log
for %%J in (elevenlabs-it-eng planetdepos-lead-devops chla-cloudops tential-lead-cloud bae-windows-devops mantech-aws-cloud-admin cai-role-tbd sundayy-role-tbd) do (
  echo. >> tool_test_output.log
  echo ########## --only=%%J ########## >> tool_test_output.log
  python run_url.py --dry --only=%%J >> tool_test_output.log 2>&1
)
echo. >> tool_test_output.log
echo ==== ALL DONE ==== >> tool_test_output.log
