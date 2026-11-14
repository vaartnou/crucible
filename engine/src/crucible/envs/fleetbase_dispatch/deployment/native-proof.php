<?php
// Private native contract probe. Part 1 replays the audited v1 workflow. Part 2 checks
// each v2 assumption in docs/fleetbase-api-audit.md ("v2 operations") and exits nonzero
// naming the first one that fails. All records are created with explicit tenant-owned
// places and deleted at the end, so count-state.php must still report a clean stack.
$token = trim(file_get_contents('/tmp/crucible-private-token'));
function api($method, $path, $body=null, $headers=[]) {
 global $token;
 $ch=curl_init('http://127.0.0.1:8000/v1'.$path);
 curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>array_merge(['Authorization: Bearer '.$token,'Content-Type: application/json','Accept: application/json'],$headers),CURLOPT_TIMEOUT=>30]);
 if($body!==null) curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($body));
 $raw=curl_exec($ch); $status=curl_getinfo($ch,CURLINFO_HTTP_CODE);
 $data=json_decode($raw,true);
 if($status<200||$status>=300){fwrite(STDERR,json_encode(['method'=>$method,'path'=>$path,'status'=>$status,'error'=>is_array($data)?($data['message']??$data['error']??$data['errors']??$data):substr($raw,0,1000)])."\n");exit(1);}
 echo json_encode(['method'=>$method,'path'=>$path,'http'=>$status,'id'=>$data['id']??null,'status'=>$data['status']??null,'dispatched'=>$data['dispatched']??null])."\n";
 return $data;
}
function check($ok, $assumption) {
 echo json_encode(['assumption'=>$assumption,'holds'=>(bool)$ok])."\n";
 if(!$ok){fwrite(STDERR,"v2 assumption failed: $assumption\n");exit(2);}
}
function assigned($order, $key) { return is_array($order[$key] ?? null) ? ($order[$key]['id'] ?? null) : null; }
function place($name, $lat, $lon) {
 return api('POST','/places',['name'=>$name,'street1'=>$name,'city'=>'Helsinki','country'=>'FI','latitude'=>$lat,'longitude'=>$lon]);
}
$location=['type'=>'Point','coordinates'=>[24.94,60.17]];
$driver=api('POST','/drivers',['internal_id'=>'crucible-proof-driver','name'=>'Crucible Driver','status'=>'active','location'=>$location]);
$vehicle=api('POST','/vehicles',['internal_id'=>'crucible-proof-vehicle','name'=>'Crucible Van','plate_number'=>'CRUCIBLE-02','status'=>'available','location'=>$location]);
$places=[];
$orders=[];
foreach([1,2,3,4] as $n){
 $pickup=place('CRUCIBLE-PROOF-'.$n.'-PLACE-0',60.17,24.94);
 $dropoff=place('CRUCIBLE-PROOF-'.$n.'-PLACE-1',60.18,24.95);
 $places[]=$pickup['id']; $places[]=$dropoff['id'];
 $body=['type'=>'transport','internal_id'=>'crucible-proof-'.$n,'pickup'=>$pickup['id'],'dropoff'=>$dropoff['id'],'meta'=>['crucible_request_id'=>'proof-'.$n],'pod_required'=>false,'adhoc'=>false];
 $order=api('POST','/orders',$body,['Idempotency-Key: crucible-proof-'.$n]);
 if($n===1){
  $replay=api('POST','/orders',$body,['Idempotency-Key: crucible-proof-'.$n]);
  if($order['id']!==$replay['id'])throw new Exception('Idempotency failed');
 }
 $orders[$n]=['id'=>$order['id'],'dropoff'=>$dropoff['id']];
}

// Part 1: audited v1 workflow on order 1.
$id=$orders[1]['id'];
$order=api('PUT','/orders/'.$id,['driver'=>$driver['id'],'vehicle'=>$vehicle['id']]);
$order=api('PATCH','/orders/'.$id.'/dispatch');
$order=api('POST','/orders/'.$id.'/start');
for($n=0;$n<5&&$order['status']!=='completed';$n++){
 $next=api('GET','/orders/'.$id.'/next-activity');
 if(!isset($next[0])) throw new Exception('No next activity');
 $order=api('POST','/orders/'.$id.'/update-activity',['activity'=>$next[0]]);
}
$order=api('GET','/orders/'.$id);
if($order['status']!=='completed')throw new Exception('Not completed');
file_put_contents('/tmp/crucible-native-order-proof.json',json_encode($order,JSON_PRETTY_PRINT));

// Part 2: v2 assumptions on orders 2 to 4.
$a=$orders[2]['id']; $b=$orders[3]['id'];
$route=['pickup'=>0,'dropoff'=>1];
api('PUT','/orders/'.$a,['driver'=>$driver['id'],'vehicle'=>$vehicle['id'],'meta'=>['crucible_request_id'=>'proof-2','crucible_route'=>$route]]);
$order=api('GET','/orders/'.$a);
check(assigned($order,'driver_assigned')===$driver['id'] && ($order['meta']['crucible_route']??null)==$route, 'PUT /orders/:id sets driver, vehicle and meta together');
api('PUT','/orders/'.$a,['meta'=>['crucible_request_id'=>'proof-2','crucible_route'=>null,'note'=>'probe']]);
$order=api('GET','/orders/'.$a);
check(($order['meta']['note']??null)==='probe' && ($order['meta']['crucible_route']??null)===null, 'PUT meta stores keys and null clears a key');
check(assigned($order,'driver_assigned')===$driver['id'], 'PUT without driver/vehicle keeps the assignment');
api('PATCH','/orders/'.$a.'/dispatch');
api('PUT','/orders/'.$b,['driver'=>$driver['id'],'vehicle'=>$vehicle['id']]);
api('PATCH','/orders/'.$b.'/dispatch');
$order=api('GET','/orders/'.$b);
check($order['dispatched']===true, 'one driver can hold several dispatched orders');
api('PUT','/orders/'.$a,['driver'=>null,'vehicle'=>null]);
$order=api('GET','/orders/'.$a);
check(assigned($order,'driver_assigned')===null && assigned($order,'vehicle_assigned')===null, 'PUT driver/vehicle null unassigns');
echo json_encode(['observation'=>'dispatched after unassign','dispatched'=>$order['dispatched']??null])."\n";
api('PUT','/orders/'.$a,['driver'=>$driver['id'],'vehicle'=>$vehicle['id']]);
$order=api('GET','/orders/'.$a);
check(assigned($order,'driver_assigned')===$driver['id'], 'reassign after unassign');
api('DELETE','/orders/'.$a.'/cancel');
$order=api('GET','/orders/'.$a);
check(($order['status']??null)==='canceled', 'DELETE /orders/:id/cancel sets status canceled');
api('DELETE','/orders/'.$b.'/cancel');
$order=api('GET','/orders/'.$b);
check(($order['status']??null)==='canceled', 'a dispatched order can be cancelled');
$c=$orders[4]['id'];
api('PUT','/orders/'.$c,['driver'=>$driver['id'],'vehicle'=>$vehicle['id']]);
api('PATCH','/orders/'.$c.'/dispatch');
api('POST','/orders/'.$c.'/start');
api('DELETE','/orders/'.$c.'/cancel');
$order=api('GET','/orders/'.$c);
check(($order['status']??null)==='canceled', 'a started order can be cancelled (customer cancels cargo on board)');
api('PUT','/places/'.$orders[3]['dropoff'],['latitude'=>60.19,'longitude'=>24.96]);
$place=api('GET','/places/'.$orders[3]['dropoff']);
$coords=$place['location']['coordinates']??[$place['longitude']??null,$place['latitude']??null];
check(abs(($coords[1]??0)-60.19)<1e-7 && abs(($coords[0]??0)-24.96)<1e-7, 'PUT /places/:id moves the place');
foreach(['out_of_service','inactive','available'] as $status){
 api('PUT','/vehicles/'.$vehicle['id'],['status'=>$status]);
 $read=api('GET','/vehicles/'.$vehicle['id']);
 check(($read['status']??null)===$status, 'PUT /vehicles/:id status '.$status);
}

foreach($orders as $item) api('DELETE','/orders/'.$item['id']);
foreach($places as $place_id) api('DELETE','/places/'.$place_id);
api('DELETE','/vehicles/'.$vehicle['id']);
api('DELETE','/drivers/'.$driver['id']);
echo "native v1 workflow and v2 assumptions hold; physical verification remains simulator responsibility\n";
