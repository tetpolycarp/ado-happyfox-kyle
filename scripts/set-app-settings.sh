#!/usr/bin/env bash
# ---------------------------------------------------------------
# Sets all new externalized config App Settings on the Function App.
# Run from a terminal where you are already logged in to Azure CLI.
#
# Usage:  bash scripts/set-app-settings.sh
# ---------------------------------------------------------------

set -euo pipefail

APP_NAME="xx-prod-cus-func-supporttickets01"
RG="xx-prod-cus-supporttickets"

echo "Setting ADO custom field references..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    ADO_FIELD_CLIENT_REQUESTED="Custom.ClientRequested" \
    ADO_FIELD_CLIENT_SELECTION_PORTAL="Custom.ClientSelectionforPortalVisibility" \
    ADO_FIELD_REQUEST_CATEGORY="Custom.RequestCategory" \
    ADO_FIELD_TEST_SCENARIOS="Custom.TestScenarios" \
    ADO_FIELD_UAT_FEEDBACK_RESOLVED="Custom.UATFeedbackResolved" \
    ADO_FIELD_UI_UX_ACCEPTANCE_CRITERIA="Custom.UIandUXAcceptanceCriteria" \
    ADO_FIELD_IS_NEW_FUNCTIONALITY="Custom.IsNewFunctionality" \
    ADO_FIELD_UAT_DEPLOYMENT_STATUS="Custom.UATDeploymentStatus" \
    ADO_FIELD_SYNC_TO_CLIENT_PORTAL="Custom.SyncToClientPortal" \
    ADO_FIELD_PRODUCTION_RELEASE_DATE="Custom.ProductionReleaseDate" \
    ADO_FIELD_RELEASE_VERSION="Custom.ReleaseVersion" \
    ADO_FIELD_INTEGRATION_SPRINT="Custom.IntegrationSprint" \
    ADO_FIELD_SCRUM_TEAM="Custom.ScrumTeam" \
    ADO_FIELD_UAT_ENV_DEPLOYMENT_STATUS="Custom.UATEnvironmentDeploymentStatus" \
    ADO_FIELD_SUPPORT_TICKET_NUMBER="Custom.SupportTicketNumber" \
    ADO_FIELD_SUPPORT_TICKET_STATUS="Custom.SupportTicketStatus" \
    ADO_FIELD_SUPPORT_TICKET_TITLE="Custom.SupportTicketTitle" \
    ADO_FIELD_ADO_PARENT_ID="Custom.ADOParentID" \
    ADO_FIELD_REQUIREMENTS_APPROVAL_STATUS="Custom.RequirementsApprovalStatus" \
    ADO_FIELD_RELEASE_APPROVAL="Custom.ReleaseApproval" \
    ADO_FIELD_RELEASE_NOTES="Custom.ReleaseNotes" \
    ADO_FIELD_CONTRACT_REQUIREMENT_NUMBERS="Custom.ContractRequirementNumbers" \
    ADO_FIELD_LATEST_SYNC_DATE="Custom.LatestSyncDate" \
  --output none

echo "Setting ADO trigger values..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    ADO_TRIGGER_STATE_READY="Ready for Development" \
    ADO_TRIGGER_SYNC_NOW="Sync Now" \
    ADO_TRIGGER_UAT_IN_ENVIRONMENT="In UAT Environment" \
    ADO_SYNC_TO_CLIENT_PORTAL_RESET_VALUE="None" \
    CHILD_DEDUP_WINDOW_SECONDS=10 \
  --output none

echo "Setting HappyFox default settings..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    HF_DEFAULT_CATEGORY_ID=2 \
    HF_DEFAULT_PRODUCT_ID=69 \
    HF_DEFAULT_PRIORITY_NAME="Moderate" \
    HF_DEFAULT_STATUS_ID=1 \
    HF_CREATE_USER_NAME="Avaratak" \
    HF_CREATE_USER_EMAIL="lori.tragesser+test@brandtinfo.com" \
    HF_UPDATE_USER_NAME="Kyle Bring" \
    HF_UPDATE_USER_EMAIL="kyle.bring@brandtinfo.com" \
    HF_STAFF_MATCH_PATTERNS="kyle.bring,avaratak,lori.tragesser+test" \
  --output none

echo "Setting HappyFox custom field IDs..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    HF_CF_REQUEST_CATEGORY="t-cf-2" \
    HF_CF_SCRUM_TEAM="t-cf-3" \
    HF_CF_CLIENT_REQUESTED="t-cf-5" \
    HF_CF_PRODUCT="t-cf-8" \
    HF_CF_DEV_TICKET_NUMBER="t-cf-29" \
    HF_CF_RELEASE_VERSION="t-cf-39" \
    HF_CF_CONTRACT_REQUIREMENTS="t-cf-37" \
    HF_CF_ISSUE_TYPE_DEV="t-cf-38" \
    HF_CF_REQUIREMENTS_ACCEPTANCE="t-cf-40" \
    HF_CF_UAT_STATUS="t-cf-41" \
    HF_CF_PARENT="t-cf-42" \
    HF_CF_DEV_PARENT_NUMBER="t-cf-45" \
    HF_CF_ADO_PROJECT="t-cf-62" \
    HF_CF_ADO_WORK_ITEM_TYPE="t-cf-63" \
    HF_CF_ADO_WORK_ITEM_TITLE="t-cf-64" \
    HF_CF_ADO_TICKET_STATE="t-cf-65" \
  --output none

echo "Setting JSON mapping: Client → HappyFox choice ID..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_CLIENT_TO_HF='{"Alabama Parks":62,"Arkansas Parks":130,"Avaratak":167,"Bahamas H/F":128,"Colorado H/F and Parks":114,"Florida H/F":132,"Georgia H/F and Parks":154,"Georgia Power":143,"Idaho H/F":145,"Idaho Parks":140,"Indiana H/F":136,"Iowa H/F":152,"Kansas H/F":151,"Lake Casitas Parks":138,"Louisiana Parks":129,"Maryland H/F":146,"Massachusetts H/F":149,"Muskingum Parks":135,"Nebraska H/F":137,"North Carolina H/F":126,"Oklahoma H/F":148,"Oregon H/F":142,"South Carolina H/F":141,"South Carolina Parks":153,"South Dakota H/F and Parks":127,"Tennessee H/F":133,"Tennessee Parks":150,"USVI H/F":131,"Virginia H/F":147,"Washington H/F":144,"West Virginia H/F":134}' \
  --output none

echo "Setting JSON mapping: Project ID → Product..."
# Keys are ADO project GUIDs from resourceContainers.project.id in webhook payloads.
# To find your project GUID: az devops project show --project "YourProject" --query id
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_PROJECT_TO_PRODUCT='{"784b1506-8cc0-486f-98e9-046b42f548b1":68,"2d1cd9d1-e751-4d14-b90a-20a80321d588":69,"739f806f-b632-41f0-8dcd-db00f41d32d6":67,"c5dbfd83-cc5b-4468-b963-40042cbbc0dd":69}' \
  --output none

echo "Setting JSON mapping: Request Category..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_REQUEST_CATEGORY='{"Configuration":20,"Cosmetic Improvement":193,"Customization / Improvement":248,"Data Change/Update":17,"Data Migration / Conversion":198,"Data Request / Query":23,"Database / Schema Change":199,"Deployment / Network / Infrastructure":196,"Documentation":22,"Edit Report":7,"Implementation":119,"Internal Admin":194,"Legislative/Rule Change":11,"Marketing":15,"New Report":16,"New System Feature":24,"Performance Improvement / Optimization":190,"Production Bug":18,"Security Improvement":192,"Standing Service Order":6,"Technical Debt":191,"Test Defect / Bug":13,"Workflow / Feature Improvement":195}' \
  --output none

echo "Setting JSON mapping: Priority..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_PRIORITY='{"1":2,"2":3,"3":1,"4":4}' \
  --output none

echo "Setting JSON mapping: Scrum Team..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_SCRUM_TEAM='{"GO Customers and Vehicles":37,"GO Licensing":28,"GO Mobile":26,"GO Payments":36,"GO Platform":31,"GO Reservations":33,"Itinio":222,"Terra East":35,"Terra West":30}' \
  --output none

echo "Setting JSON mapping: UAT Status..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_UAT_STATUS='{"Failed UAT":160,"In UAT Environment":197,"Not in UAT":161,"Not in UAT Environment":161,"Passed UAT / Ready for Production":159}' \
  --output none

echo "Setting JSON mapping: Requirements Acceptance..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_REQUIREMENTS_ACCEPTANCE='{"Modification to Requirements Requested":158,"Modifications to Requirements Requested":158,"Requirements Accepted":155,"Requirements Not Submitted":156,"Requirements Not Yet Submitted":156,"Requirements Pending Acceptance":157}' \
  --output none

echo "Setting JSON mapping: Client Alias (for tagged content parsing)..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_CLIENT_ALIAS='{"ADCNR":"Alabama Parks","ADPHT":"Arkansas Parks","CPW":"Colorado H/F and Parks","GOF":"Florida H/F","GADNR":"Georgia H/F and Parks","GAPOWER":"Georgia Power","BAHAMAS":"Bahamas H/F","IDFG":"Idaho H/F","IDPR":"Idaho Parks","IDNR":"Indiana H/F","IOWA":"Iowa H/F","KDWP":"Kansas H/F","LASP":"Louisiana Parks","MDDNR":"Maryland H/F","MassDFG":"Massachusetts H/F","MWCD":"Muskingum Parks","NGPC":"Nebraska H/F","NCWRC":"North Carolina H/F","ODWC":"Oklahoma H/F","ODFW":"Oregon H/F","SCDNR":"South Carolina H/F","SCPRT":"South Carolina Parks","SDGFP":"South Dakota H/F and Parks","TSP":"Tennessee Parks","TWRA":"Tennessee H/F","VIDPNR":"USVI H/F","GOV":"Virginia H/F","WDFW":"Washington H/F","WVDNR":"West Virginia H/F","Alabama Parks":"Alabama Parks","Arkansas Parks":"Arkansas Parks","Avaratak":"Avaratak","Bahamas H/F":"Bahamas H/F","Colorado H/F and Parks":"Colorado H/F and Parks","Florida H/F":"Florida H/F","Georgia H/F and Parks":"Georgia H/F and Parks","Georgia Power":"Georgia Power","Idaho H/F":"Idaho H/F","Idaho Parks":"Idaho Parks","Indiana H/F":"Indiana H/F","Iowa H/F":"Iowa H/F","Kansas H/F":"Kansas H/F","Lake Casitas Parks":"Lake Casitas Parks","Louisiana Parks":"Louisiana Parks","Maryland H/F":"Maryland H/F","Massachusetts H/F":"Massachusetts H/F","Muskingum Parks":"Muskingum Parks","Nebraska H/F":"Nebraska H/F","North Carolina H/F":"North Carolina H/F","Oklahoma H/F":"Oklahoma H/F","Oregon H/F":"Oregon H/F","South Carolina H/F":"South Carolina H/F","South Carolina Parks":"South Carolina Parks","South Dakota H/F and Parks":"South Dakota H/F and Parks","Tennessee H/F":"Tennessee H/F","Tennessee Parks":"Tennessee Parks","USVI H/F":"USVI H/F","Virginia H/F":"Virginia H/F","Washington H/F":"Washington H/F","West Virginia H/F":"West Virginia H/F"}' \
  --output none

echo "Setting JSON mapping: Issue Type - Development..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_ISSUE_TYPE_DEV='{"Bug":122,"Epic":123,"Initiative":125,"Story":124}' \
  --output none

echo "Setting JSON mapping: State → Child State (per parent type)..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_STATE_TO_CHILD_STATE='{"Epic":{"New":"New","Backlog":"New","Open":"In Progress","Resolved":"Completed","In Progress":"In Progress","Completed":"Completed","Removed":"Removed"},"Initiative":{"New":"New","Backlog":"Backlog","In Progress":"In Progress","Completed":"Completed","Removed":"Removed"},"Feature":{"New":"New","Backlog":"Backlog","In Progress":"In Progress","Completed":"Completed","Removed":"Removed"}}' \
  --output none

echo "Setting JSON mapping: State → HappyFox Status ID (per parent type)..."
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --settings \
    MAPPING_STATE_TO_HF_STATUS='{"User Story":{"New":5,"Ready for Refinement":8,"Ready for Development":7,"Backlog":3,"Intake - Requirements":8,"Intake - Product Review":8,"Intake - Tech Scope":8,"Resolved":4,"In Progress":13,"Selected for Development":13,"Pull Request":13,"Ready for QA":14,"Passes QA Testing":14,"Completed":4},"Bug":{"New":5,"Ready for Refinement":8,"Ready for Development":7,"Backlog":3,"Intake - Requirements":8,"Intake - Product Review":8,"Intake - Tech Scope":8,"Resolved":4,"In Progress":13,"Selected for Development":13,"Pull Request":13,"Ready for QA":14,"Passes QA Testing":14,"Completed":4},"Epic":{"New":5,"Backlog":3,"Open":4,"Resolved":4,"In Progress":13,"Ready for Testing":14,"Completed":4,"Removed":4},"Initiative":{"New":5,"Backlog":3,"Resolved":4,"In Progress":13,"Completed":4,"Removed":4},"Feature":{"New":5,"Backlog":3,"In Progress":13,"Completed":4,"Removed":4}}' \
  --output none

echo ""
echo "All app settings configured. The Function App will restart automatically."
echo "Verify with: az functionapp config appsettings list --name $APP_NAME --resource-group $RG --query \"[].name\" -o tsv | sort"
